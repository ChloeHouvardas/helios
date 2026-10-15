"""helios-collect — the AI receivables agent, assembled on the Helios platform.

    python3 app.py                       # narrated run over every scenario
    python3 app.py --list                # what each scenario demonstrates
    python3 app.py --scenario INV-1006   # one control path
    python3 app.py --json                # machine-readable
    python3 app.py --trace               # per-step trace
    python3 app.py --dashboard           # the book, as the summary page would show it

What this file is
-----------------
A runnable Helios app answering the flagship requirement in
`specs/helios-context.md` §"Helios Collect: AI Receivables Agent": chase payments,
negotiate reasonable terms when a customer has not paid, and direct money to the
right place when it arrives.

It is a prototype — no web server, no database engine, a CLI owner — because the
point is the wiring and the controls, not the chrome.

What it composes (nothing here is invented)
-------------------------------------------
1. `agent_integration` (platform/agent-integration) — the gated agent loop, the
   audit bridge, and the transports. This app *runs through* the platform rather
   than beside it: `build_integration()` returns an auditor, a bridge and a
   runtime already connected, and every side effect this app takes goes through
   them. Note that `entry-point/references/platform-status.md` says no agent loop
   exists; that file predates this package and is now stale (verified 2026-10-15).
2. `audit_agent` (platform/audit-agent-builder) — the standalone auditor, built
   from `agents/helios-collect-audit.yaml`, with this app's fact source.
3. `agent_toolkit` (platform/action-agent-builder) — the tool contract, the store,
   the action log, and the `send_email` handler reused as-is.
4. `fin_tools` (platform/financial-tools) — the payments ledger and the knowledge
   graph, for real money movement and cross-app state.

The design principle, enforced below rather than asserted: **the model proposes,
code decides.**

Which gate is authoritative
---------------------------
`agent_toolkit`'s in-process `AuditGate` is not used. `agent_integration`'s bridge
makes the standalone auditor authoritative, which is the resolution that package's
README proposes for the overlap. That is still an *unresolved architectural seam*
rather than a settled design — the two gates overlap and the team has not agreed
which wins. This app inherits the choice from the platform package it composes
rather than making it silently; see README.md and BUILD.md step 5.

Where the human lands
---------------------
Two distinct human checkpoints, and they are not the same thing:

  * **Hold resolution** (`Owner.decide_hold`) — after the auditor holds a proposal
    that exceeded a limit. The owner sees the auditor's own `owner_question`, never
    a summary the agent wrote about itself.
  * **Approving is not the same as being asked.** Three scenarios are DENIED, so the
    owner is never consulted about them at all. The default owner below approves
    everything it is shown, and those three still do not happen. That is the
    platform's central claim demonstrated rather than asserted: the control does not
    depend on the human noticing, and it does not depend on the agent behaving.

Nothing in this file sets a dummy signing key. With no
`HELIOS_AUDIT_SIGNING_KEY` in the environment, `build_integration()` generates an
ephemeral one so grants are still *signed* — the more honest demo, and one that
cannot be shipped by accident. The bundle reports `ephemeral_key` and this app
prints a warning when it is set.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

HERE = Path(__file__).resolve().parent
# Repo root for source-on-path runs. Derived defensively, NOT as parents[1]:
# in a deployed container this file is at /app/app.py, where Path("/app") has
# only one parent, so parents[1] raises IndexError at IMPORT time. That kills
# the process before uvicorn can serve /health, and the deploy smoke test fails
# with an opaque traceback instead of a readable missing-route error.
REPO = HERE.parents[1] if len(HERE.parents) > 1 else HERE

# The four platform packages. A generated app would declare these as dependencies;
# this one imports them in place so it runs with no install step. Each entry is
# guarded by is_dir() because inside the golden base image the packages are
# pip-installed and these directories do not exist at all.
for _p in (
    REPO / "platform" / "audit-agent-builder" / "src",
    REPO / "platform" / "action-agent-builder" / "src",
    REPO / "platform" / "agent-integration" / "src",
    REPO / "platform" / "financial-tools",
):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from agent_integration.wiring import build_integration  # noqa: E402
from agent_toolkit.manifest import load_manifest as load_action_manifest  # noqa: E402
from audit_agent.facts import FactResolver  # noqa: E402
from audit_agent.manifest import AuditManifest  # noqa: E402
from fin_tools import build_world  # noqa: E402

import config  # noqa: E402
import tools as app_tools  # noqa: E402
from book import Book  # noqa: E402
from facts import CollectFactSource  # noqa: E402

AUDIT_MANIFEST = HERE / "agents" / "helios-collect-audit.yaml"
ACTION_MANIFEST = HERE / "agents" / "helios-collect-action.json"

TASK_REMINDER = "collect.reminder"
TASK_NEGOTIATE = "collect.negotiate_terms"
TASK_APPLY_PAYMENT = "collect.apply_payment"


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

@dataclass
class App:
    """A running helios-collect: its book, its auditor, its loop, its owner."""

    book: Book
    fact_source: CollectFactSource
    integration: Any
    runtime: Any
    action_manifest: Any
    audit_manifest: AuditManifest
    bundle: Any
    #: Where the auditor's hash-chained evidence actually lands. Computed rather
    #: than read from the manifest: `build_integration()` resolves its own
    #: `log_target` from `data_dir` and passes it to `build_audit_agent()`, which
    #: overrides the manifest's `settings.log_path`. Reporting the manifest's value
    #: would point a reviewer at a file that does not exist.
    audit_log_path: Path = Path("audit.jsonl")
    principal: str = "owner"

    @property
    def app_id(self) -> str:
        return self.audit_manifest.app_id

    def task_types(self) -> list[str]:
        return sorted(self.audit_manifest.tasks)

    def tools_for(self, task_type: str) -> list[str]:
        return list(self.audit_manifest.tasks[task_type].allowed_tools)

    def close(self) -> None:
        self.integration.close()


def build_app(run_dir: Path | str | None = None, *, data_dir: Path | str | None = None,
              today: Any = None) -> App:
    """Assemble the app from its manifests, data and the platform packages.

    This is the wiring a scaffolder would generate: read the spec, load both
    manifests, build the auditor over the app's own fact source, expose the tools,
    and run the gated loop inside the app process.
    """
    book = Book.load(HERE / "data", today=today) if data_dir is None else Book.load(data_dir, today=today)

    # The platform's financial services: the ledger inbound money lands in, and
    # the shared graph other Helios apps read. Handlers are called on the instances
    # rather than through the registry — see tools.py for why that matters.
    bundle = build_world()
    for account in config.ACCOUNTS:
        bundle.world.balances.setdefault(account, 0)
    bundle.world.balances.setdefault(config.INBOUND_ROUTING.get("CUST-003", ""), 0)

    action_manifest = load_action_manifest(ACTION_MANIFEST)
    audit_manifest = AuditManifest.load(AUDIT_MANIFEST)

    # The cross-file invariant the app-builder validator is meant to check, and the
    # single most important one in the whole build: every tool the action agent may
    # use must be declared by the audit agent. Enforced here by hand because that
    # validator does not exist yet. entry-point/checks/check_app.py does the same
    # check from the outside.
    declared = {t.name for t in audit_manifest.tools}
    offered = set(action_manifest.tools) | set(action_manifest.mandate.allowed_tools)
    undeclared = sorted(offered - declared)
    if undeclared:
        raise ValueError(
            f"action manifest offers tool(s) the audit manifest has not declared: "
            f"{undeclared}. An ungated side effect is not allowed — declare them in "
            f"{audit_manifest.app_id}'s audit manifest.")

    # And the app must actually provide every tool both manifests promise.
    provided = set(app_tools.TOOL_NAMES)
    missing = sorted(declared - provided)
    if missing:
        raise ValueError(
            f"the audit manifest declares tool(s) this app does not implement: "
            f"{missing}. A declared-but-absent tool is denied at runtime, which "
            f"fails closed but reads as a broken app.")

    run_dir = Path(run_dir) if run_dir is not None else (HERE / "runs")
    data_root = Path(data_dir) if data_dir is not None else (run_dir / "audit")
    # Passed explicitly so the app knows exactly where its tamper-evident evidence
    # lands, rather than deriving the platform's default filename.
    audit_log_path = data_root / "helios-collect-audit-log.jsonl"

    tools = app_tools.build_tools(book, bundle)

    # The platform's gated loop. `manifest_path` points at THIS app's audit
    # manifest, so authority comes from a reviewed file rather than from anything
    # the app generates for itself at runtime.
    integration = build_integration(
        store=book.store,
        tools=tools,
        app_id=audit_manifest.app_id,
        agent_id=action_manifest.name,
        agent_name=action_manifest.name,
        instructions=action_manifest.instructions,
        manifest_path=AUDIT_MANIFEST,
        runs_dir=run_dir,
        data_dir=data_root,
        audit_log_path=audit_log_path,
        contact_window_days=config.CONTACT_WINDOW_DAYS,
    )

    # Swap in this app's fact source. The platform's HeliosStoreFactSource cannot
    # supply due-ness, disputes, credit exposure or cents-denominated amounts, and
    # a fact-bound rule whose fact never resolves denies everything. Replacing the
    # resolver rather than editing the package keeps this app out of other agents'
    # code; `AuditAgent.facts` is an injected attribute for exactly this reason.
    fact_source = CollectFactSource(book, contact_window_days=config.CONTACT_WINDOW_DAYS)
    integration.audit_agent.facts = FactResolver(
        fact_source, contact_window_days=config.CONTACT_WINDOW_DAYS)
    integration.runtime.fact_source = fact_source
    integration.fact_source = fact_source
    integration.runtime.max_turns = action_manifest.max_turns

    return App(
        book=book, fact_source=fact_source, integration=integration,
        runtime=integration.runtime, action_manifest=action_manifest,
        audit_manifest=audit_manifest, bundle=bundle,
        audit_log_path=audit_log_path,
    )


# ---------------------------------------------------------------------------
# The owner — a human checkpoint, not part of the agent
# ---------------------------------------------------------------------------

class Owner(Protocol):
    """The human in the loop.

    A deployed app surfaces these through the `approval_queue` UI pattern
    (specs/app-builder.md), which BUILD.md singles out as the page that matters
    most: every side-effect action can be held, and without a queue the agents
    pause invisibly.
    """

    def decide_hold(self, hold: Mapping[str, Any]) -> bool:
        """Approve or decline a proposal the auditor held."""


class ScriptedOwner:
    """A stand-in owner so the app runs offline and deterministically.

    Deliberately **permissive**: it approves nearly everything it is shown. That is
    the point, not an oversight. Three scenarios are denied in code and never reach
    this object at all, so a busy owner rubber-stamping their way through the queue
    still cannot make them happen. The control does not depend on the human
    noticing.

    The two declines encode ordinary business judgement rather than suspicion:
      * a 45-day extension is longer than this business offers, so it is declined
        even though the agent was entitled to ask;
      * a payment larger than the invoice is worth is declined because the reference
        should be checked against the bank feed before anything is booked.
    """

    def __init__(self, *, verbose: bool = True) -> None:
        self.verbose = verbose
        self.decisions: list[dict[str, Any]] = []
        self.asked: list[str] = []

    def _say(self, text: str) -> None:
        if self.verbose:
            print(text)

    def decide_hold(self, hold: Mapping[str, Any]) -> bool:
        question = str(hold.get("owner_question") or hold.get("reason") or "")
        lowered = question.lower()
        self.asked.append(str(hold.get("hold_id")))
        approve = True
        if "45 days" in lowered or "extension longer than 14 days" in lowered:
            approve = False   # this business does not offer 45-day terms
        if "exceeds the recorded invoice amount" in lowered:
            approve = False   # check the bank reference before booking it
        self._say(f"    [owner asked] {question.strip()}")
        self._say(f"    [owner decides] {'approve' if approve else 'decline'}")
        self.decisions.append({
            "hold_id": hold.get("hold_id"), "tool": hold.get("tool"),
            "approved": approve,
        })
        return approve


class InteractiveOwner:
    """A real owner at a terminal, for `--interactive`."""

    def __init__(self) -> None:
        self.decisions: list[dict[str, Any]] = []
        self.asked: list[str] = []

    def decide_hold(self, hold: Mapping[str, Any]) -> bool:
        question = str(hold.get("owner_question") or hold.get("reason") or "")
        self.asked.append(str(hold.get("hold_id")))
        print(f"\n  HELD  {hold.get('tool')}  (hold id: {hold.get('hold_id')})")
        print(f"        {question.strip()}")
        try:
            answer = input("        approve this exact action? [y/N] ")
        except EOFError:
            answer = "n"
        approve = answer.strip().lower() in {"y", "yes"}
        self.decisions.append({
            "hold_id": hold.get("hold_id"), "tool": hold.get("tool"),
            "approved": approve,
        })
        return approve


# ---------------------------------------------------------------------------
# The scenarios: one control path each
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Scenario:
    """One invoice, one task, one control path.

    `calls` is what the agent asks for. It is a fixed script rather than a model's
    output on purpose — the thing being demonstrated is the gate, not the reasoning,
    and a script can include requests that *should* be refused without hoping a
    model decides to misbehave.
    """

    invoice_id: str
    task_type: str
    calls: tuple[tuple[str, dict[str, Any]], ...]
    control: str
    expects: str
    note: str = ""


def scenarios() -> list[Scenario]:
    """The demo script. Each entry exercises exactly one rule."""
    return [
        Scenario(
            invoice_id="INV-1001", task_type=TASK_REMINDER,
            calls=(("send_email", {
                "customer_id": "CUST-001", "invoice_id": "INV-1001",
                "subject": "Invoice RL-2026-047 is 21 days overdue",
                "body": ("Dear Dana, our records show invoice RL-2026-047 for "
                         "4200.00 CNY fell due 21 days ago and remains unpaid. "
                         "Could you let us know when we can expect payment?"),
            }),),
            control="none — the normal path",
            expects="done",
            note="21 days late, first contact this week, under every threshold.",
        ),
        Scenario(
            invoice_id="INV-1002", task_type=TASK_REMINDER,
            calls=(("send_email", {
                "customer_id": "CUST-002", "invoice_id": "INV-1002",
                "subject": "Following up on invoice MS-2026-112",
                "body": ("Dear Priya, invoice MS-2026-112 for 850.00 CNY is still "
                         "outstanding. Please arrange payment."),
            }),),
            control="app.contact_frequency",
            expects="refused",
            note="This customer was already contacted twice in 7 days. The count "
                 "comes from recorded history, so describing them as untouched "
                 "would not help.",
        ),
        Scenario(
            invoice_id="INV-1003", task_type=TASK_REMINDER,
            calls=(("send_email", {
                "customer_id": "CUST-003", "invoice_id": "INV-1003",
                "subject": "Invoice HL-2026-078 is overdue",
                "body": ("Dear Wei, invoice HL-2026-078 for 12500.00 CNY fell due "
                         "5 days ago. We would appreciate an update."),
            }),),
            control="app.large_invoice_approval",
            expects="done",
            note="Over the 10,000 threshold. The amount is a FACT read from the "
                 "ledger, so writing a smaller number in the email body would not "
                 "slip it under.",
        ),
        Scenario(
            invoice_id="INV-1004", task_type=TASK_NEGOTIATE,
            calls=(("send_email", {
                "customer_id": "CUST-005", "invoice_id": "INV-1004",
                "subject": "Re: invoice NG-2026-233 — your request for more time",
                "body": ("Dear Elena, thank you for letting us know. We can offer "
                         "an extension of 45 days on invoice NG-2026-233."),
                "extension_offer_days": 45,
            }),),
            control="app.extension_offer_limit",
            expects="declined_by_owner",
            note="45 days is beyond the agent's own 14-day authority, so it goes to "
                 "the owner — who declines it as business judgement, not suspicion.",
        ),
        Scenario(
            invoice_id="INV-1005", task_type=TASK_NEGOTIATE,
            calls=(
                ("send_email", {
                    "customer_id": "CUST-005", "invoice_id": "INV-1005",
                    "subject": "Re: invoice NG-2026-241 — agreed terms",
                    "body": ("Dear Elena, we can extend the due date on invoice "
                             "NG-2026-241 by 10 days as you asked."),
                    "extension_offer_days": 10,
                }),
                ("record_agreed_extension", {
                    "invoice_id": "INV-1005", "extension_days": 10,
                    "terms_note": ("Customer asked for more time on the phone; "
                                   "agreed 10 days from the current due date."),
                }),
            ),
            control="none — inside the agent's own authority",
            expects="done",
            note="10 days is within the cap, so the owner is not bothered. Paired "
                 "with INV-1004 to show the threshold is a threshold.",
        ),
        Scenario(
            invoice_id="INV-1006", task_type=TASK_REMINDER,
            calls=(("send_email", {
                "customer_id": "CUST-004", "invoice_id": "INV-1006",
                "subject": "Invoice CI-2026-088 is 18 days overdue",
                "body": ("Dear Marcus, invoice CI-2026-088 for 1980.00 CNY is "
                         "overdue. Please arrange payment."),
            }),),
            control="app.disputed_invoice",
            expects="refused",
            note="THE DEMO. The customer disputes the work. This is a DENY, not a "
                 "hold, so the owner is never asked — and the default owner "
                 "approves everything it is shown. A collections reminder sent into "
                 "an open dispute makes things worse and no queue item fixes that.",
        ),
        Scenario(
            invoice_id="INV-1007", task_type=TASK_REMINDER,
            calls=(("send_email", {
                "customer_id": "CUST-001", "invoice_id": "INV-1007",
                "subject": "Invoice RL-2026-049",
                "body": ("Dear Dana, we are writing about invoice RL-2026-049 for "
                         "3100.00 CNY."),
            }),),
            control="app.not_yet_due",
            expects="refused",
            note="Not due for another 21 days. Due-ness is computed from the "
                 "store's own clock, so an agent working through a queue cannot "
                 "chase a customer early by describing the invoice as overdue.",
        ),
        Scenario(
            invoice_id="INV-1008", task_type=TASK_APPLY_PAYMENT,
            calls=(("record_inbound_payment", {
                "invoice_id": "INV-1008", "amount_cents": 45000,
                "currency": "CNY", "reference": "BANK-REF-LF-88213",
            }),),
            control="none — money directed to the configured account",
            expects="done",
            note="Paid in full. Note what is absent from the request: no account. "
                 "Routing is app configuration, so the agent cannot misdirect it.",
        ),
        Scenario(
            invoice_id="INV-1009", task_type=TASK_APPLY_PAYMENT,
            calls=(("record_inbound_payment", {
                "invoice_id": "INV-1009", "amount_cents": 2_000_000,
                "currency": "CNY", "reference": "BANK-REF-HL-99120",
            }),),
            control="app.payment_exceeds_amount_due",
            expects="declined_by_owner",
            note="The agent claims 20,000.00 arrived on a 12,500.00 invoice. The "
                 "ceiling is max_from_fact: invoice_amount_cents, resolved from the "
                 "ledger. The agent chose the number; the ledger chose the ceiling; "
                 "neither can move the other.",
        ),
        Scenario(
            invoice_id="INV-1010", task_type=TASK_NEGOTIATE,
            calls=(
                ("send_email", {
                    "customer_id": "CUST-004", "invoice_id": "INV-1010",
                    "subject": "Re: invoice CI-2026-095 — revised terms",
                    "body": ("Dear Marcus, we can extend the due date on invoice "
                             "CI-2026-095 by 7 days."),
                    "extension_offer_days": 7,
                }),
                ("record_agreed_extension", {
                    "invoice_id": "INV-1010", "extension_days": 7,
                    "terms_note": "Customer asked for a week; agreed 7 days.",
                }),
            ),
            control="app.extension_over_credit_limit",
            expects="done",
            note="7 days is inside the agent's authority, so the extension rule does "
                 "not fire — but this customer's outstanding balance already exceeds "
                 "their credit limit. Fact measured against fact: both sides come "
                 "from the store, neither from the agent.",
        ),
    ]


class ScriptedDriver:
    """A driver that replays a scenario's calls, then reports.

    It ignores history and replays its script, which is exactly what makes the
    offline demo trustworthy: it proves the *gate* works independently of what any
    model does. Substituting a real Qwen loop means replacing this object with one
    that reads `tools` and `history` and returns calls — the gate underneath does
    not change, and that separability is the architecture-defence point.

    It does not retry a held or denied call, per the action manifest's operating
    contract. Retrying a reworded variant is circumventing a control, and it is the
    specific failure mode a reviewer looks for.
    """

    def __init__(self, calls: Sequence[tuple[str, dict[str, Any]]],
                 final_text: str = "") -> None:
        self._calls = [(name, dict(args)) for name, args in calls]
        self._index = 0
        self.final_text = final_text or "Work on this invoice is complete."

    def next_calls(self, *, task: str, instructions: str, tools: Sequence[Any],
                   history: Sequence[Mapping[str, Any]]) -> Any:
        from agent_integration.runtime import ToolCall

        if self._index >= len(self._calls):
            return self.final_text
        name, args = self._calls[self._index]
        self._index += 1
        return [ToolCall(name=name, arguments=args)]


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    """What happened to one scenario, in the shape the UI pages would consume."""

    invoice_id: str
    task_type: str
    control: str
    expects: str
    status: str
    detail: str = ""
    run_id: str = ""
    actions: list[dict[str, Any]] = field(default_factory=list)
    holds: list[dict[str, Any]] = field(default_factory=list)
    granted_tools: list[str] = field(default_factory=list)
    withheld_tools: list[str] = field(default_factory=list)
    owner_decisions: list[dict[str, Any]] = field(default_factory=list)
    action_log_path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "invoice_id": self.invoice_id, "task_type": self.task_type,
            "control": self.control, "expected": self.expects,
            "status": self.status, "detail": self.detail, "run_id": self.run_id,
            "granted_tools": self.granted_tools,
            "withheld_tools": self.withheld_tools,
            "holds": self.holds, "owner_decisions": self.owner_decisions,
            "actions": [
                {"tool": a.get("tool"), "verdict": (a.get("verdict") or {}).get("decision"),
                 "rule": (a.get("verdict") or {}).get("rule"),
                 "reason": (a.get("verdict") or {}).get("reason"),
                 "executed": a.get("executed")}
                for a in self.actions
            ],
            "action_log_path": self.action_log_path,
        }


def run_scenario(app: App, scenario: Scenario, owner: Owner, *,
                 run_id: str | None = None) -> Outcome:
    """Run one scenario through the platform's gated loop, resolving holds."""
    from agent_integration.runtime import COMPLETED

    invoice = app.book.get_invoice(scenario.invoice_id) or {}
    customer = app.book.get_customer(str(invoice.get("customer_id"))) or {}
    task = (f"{scenario.task_type} for invoice {scenario.invoice_id} "
            f"({invoice.get('number', '?')}) belonging to "
            f"{customer.get('name', 'an unknown customer')}")

    # Request every tool the app has. The auditor grants only what this task's
    # allowlist permits, and the loop offers the agent nothing else — an ungranted
    # tool is invisible rather than rejected after the fact.
    result = app.runtime.run(
        driver=ScriptedDriver(scenario.calls),
        task=task, task_type=scenario.task_type, run_id=run_id,
        requested_tools=list(app_tools.TOOL_NAMES),
    )

    outcome = Outcome(
        invoice_id=scenario.invoice_id, task_type=scenario.task_type,
        control=scenario.control, expects=scenario.expects,
        status=result.status, detail=result.detail, run_id=result.run_id,
        actions=list(result.actions), holds=list(result.holds),
        granted_tools=list(result.session.allowed_tools) if result.session else [],
        withheld_tools=list(result.session.denied_tools) if result.session else [],
        action_log_path=result.action_log_path,
    )

    # A hold stops the loop. Resolve it with the owner, then resume — which
    # executes exactly the action the owner was shown and nothing else.
    final_status = result.status
    for hold in list(result.holds):
        approve = owner.decide_hold(hold)
        outcome.owner_decisions.append({"hold_id": hold.get("hold_id"),
                                        "approved": approve})
        resumed = app.runtime.resume(
            driver=ScriptedDriver([], final_text="Resumed after the owner's decision."),
            run_id=result.run_id, hold_id=str(hold.get("hold_id")),
            decision=approve, decided_by=app.principal,
            task=task, task_type=scenario.task_type,
        )
        outcome.actions.extend(resumed.actions)
        final_status = resumed.status
        # The pre-hold detail describes a pause that is now over. Keeping it would
        # report a completed run as "paused for an owner decision".
        outcome.detail = resumed.detail if resumed.status != COMPLETED else ""
        outcome.holds = [h for h in outcome.holds
                         if h.get("hold_id") != hold.get("hold_id")]
        outcome.action_log_path = resumed.action_log_path or outcome.action_log_path

    # The loop's status says how the RUN ended, not what the WORK achieved. A
    # denial is fed back to the model as an observation and the loop then finishes
    # normally, so a run whose only side effect was refused still reports
    # COMPLETED. Deriving the outcome from the actions is what makes the demo
    # readable and the assertions meaningful. `final_status` is the status of the
    # LAST run, which after a resume is the resumed one — passing the pre-resume
    # status here would report an approved-and-executed action as still held.
    outcome.status = _summarize(outcome, fallback=final_status)
    return outcome


def _summarize(outcome: Outcome, *, fallback: str) -> str:
    """One word for what happened to this scenario, derived from its actions.

    A scenario can accumulate several verdicts: a hold, then an approve on resume.
    The question the owner and the tests care about is what FINALLY happened, so
    precedence runs from the most terminal outcome backwards:

      1. an error beats everything — something broke;
      2. a reject beats an execution — if any action was refused, the scenario did
         not complete cleanly, whether a rule refused it or the owner declined it;
      3. an execution means the work landed;
      4. a still-pending hold means the scenario is paused;
      5. otherwise nothing had any effect at all.

    Note what this deliberately does NOT do: it never reports `done` merely
    because the loop finished. A run whose only side effect was refused finishes
    normally, and calling that a success would hide the control firing.
    """
    from agent_integration.runtime import ERROR, HELD

    if fallback == ERROR:
        return "error"
    side_effects = [a for a in outcome.actions if (a.get("verdict") or {})]
    if not side_effects:
        return "read_only"
    verdicts = [str((a.get("verdict") or {}).get("decision")) for a in side_effects]
    executed = [a for a in side_effects if a.get("executed")]

    # An owner-declined hold is recorded as a reject on resume; only the original
    # hold carries the hold_id, so the owner's own decision is what distinguishes
    # "the owner said no" from "the rules said no".
    if "reject" in verdicts:
        declined = any(d.get("approved") is False for d in outcome.owner_decisions)
        return "declined_by_owner" if declined else "refused"
    if executed:
        return "done"
    if "hold" in verdicts:
        return "held" if fallback == HELD else "held_unresolved"
    return "no_effect"


# ---------------------------------------------------------------------------
# The pages (specs/app-builder.md UI patterns), rendered as a CLI
# ---------------------------------------------------------------------------

def render_dashboard(app: App, outcomes: Sequence[Outcome]) -> str:
    """The `summary` pattern: headline numbers and status."""
    snapshot = app.book.snapshot()
    unpaid = [r for r in snapshot["invoices"]
              if str(r.get("status")).lower() not in {"paid", "settled", "void"}]
    overdue = [r for r in unpaid if (r.get("days_overdue") or 0) > 0]
    total = sum(int(app.book.amount_due_cents(r["invoice_id"]) or 0) for r in unpaid)
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    lines = [
        "  DASHBOARD",
        f"    as of                {snapshot['today']}",
        f"    invoices             {len(snapshot['invoices'])}",
        f"    unpaid               {len(unpaid)}",
        f"    overdue              {len(overdue)}",
        f"    outstanding          {total / 100:.2f} CNY",
        f"    emails sent          {len(app.book.store.sent_emails)}",
        f"    ledger balance       "
        f"{app.bundle.payments.balance(config.RECEIVABLES_ACCOUNT) / 100:.2f} CNY "
        f"in {config.RECEIVABLES_ACCOUNT}",
        f"    graph nodes          {len(app.bundle.graph.nodes)}",
        f"    run outcomes         {counts}",
    ]
    return "\n".join(lines)


def render_queue(app: App, outcomes: Sequence[Outcome]) -> str:
    """The `approval_queue` pattern: what the owner was asked and decided.

    The question shown is the auditor's own `owner_question`, never a summary the
    agent wrote about itself — BUILD.md step 7. Held questions are printed here in
    full because that text is the product: it is what a real queue would render.
    """
    lines = ["  APPROVAL QUEUE"]
    asked = [o for o in outcomes if o.owner_decisions]
    if not asked:
        lines.append("    (nothing needed your decision)")
    for o in asked:
        for hold in o.holds:
            lines.append(f"    {o.invoice_id}  {hold.get('tool')}  PENDING")
        for d in o.owner_decisions:
            lines.append(f"    {o.invoice_id}  hold {d['hold_id']}  "
                         f"{'approved' if d['approved'] else 'declined'}")

    lines.append("")
    lines.append("  Refused in code, without asking you:")
    refused = [o for o in outcomes
               if o.status in {"refused", "declined_by_owner"} and not o.owner_decisions]
    if not refused:
        lines.append("    (none)")
    for o in refused:
        reasons = [str((a.get("verdict") or {}).get("reason"))
                   for a in o.actions if (a.get("verdict") or {}).get("decision") == "reject"]
        lines.append(f"    {o.invoice_id}  {o.control}")
        for reason in reasons:
            lines.append(f"        {reason}")
    return "\n".join(lines)


def render_settings(app: App) -> str:
    """The `settings` pattern: the mandate limits, as data."""
    lines = ["  SETTINGS (from config.py — the audit manifest is authoritative)"]
    for key, value in config.describe().items():
        lines.append(f"    {key:32s} {value}")
    lines.append("")
    lines.append("  Authority the audit manifest grants:")
    for line in app.audit_manifest.summary().splitlines():
        lines.append(f"    {line}")
    return "\n".join(lines)


def render_table(app: App) -> str:
    """The `table` pattern: every invoice and its state."""
    snapshot = app.book.snapshot()
    lines = ["  INVOICES",
             f"    {'id':10s} {'customer':24s} {'amount':>12s} {'due':12s} "
             f"{'overdue':>8s}  status"]
    for row in snapshot["invoices"]:
        amount = f"{row.get('amount')} {row.get('currency')}"
        days = row.get("days_overdue")
        lines.append(
            f"    {row['invoice_id']:10s} {str(row.get('customer'))[:24]:24s} "
            f"{amount:>12s} {str(row.get('due_date')):12s} "
            f"{str(days) if days is not None else '-':>8s}  {row.get('status')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="helios-collect: the AI receivables agent on Helios")
    parser.add_argument("--scenario", action="append", default=None,
                        help="run one invoice id (repeatable); default: all")
    parser.add_argument("--list", action="store_true",
                        help="list the scenarios and what each demonstrates")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable results")
    parser.add_argument("--trace", action="store_true",
                        help="print every action and verdict")
    parser.add_argument("--interactive", action="store_true",
                        help="ask a real owner about each held action")
    parser.add_argument("--dashboard", action="store_true",
                        help="print the book and stop")
    parser.add_argument("--run-dir", default=None, help="where to write run logs")
    args = parser.parse_args(argv)

    app = build_app(run_dir=args.run_dir)
    try:
        if args.list:
            print(f"helios-collect — app_id={app.app_id}")
            print(f"tasks: {', '.join(app.task_types())}\n")
            for s in scenarios():
                print(f"  {s.invoice_id}  {s.task_type}")
                print(f"      control   {s.control}")
                print(f"      expects   {s.expects}")
                if s.note:
                    print(f"      why       {s.note}")
                print()
            return 0

        if args.dashboard:
            print(render_dashboard(app, []))
            print()
            print(render_table(app))
            return 0

        owner: Owner = InteractiveOwner() if args.interactive else ScriptedOwner(
            verbose=not args.json)

        if not args.json:
            manifest = app.audit_manifest
            print(f"helios-collect — app_id={app.app_id}")
            print(f"tools: {', '.join(t.name for t in manifest.tools)}")
            print(f"rules: {', '.join(r.id for r in manifest.rules)}")
            print(f"authority: loaded from {AUDIT_MANIFEST.relative_to(REPO)}"
                  if REPO in AUDIT_MANIFEST.parents else
                  f"authority: loaded from {AUDIT_MANIFEST}")
            if app.integration.ephemeral_key:
                print("note: no HELIOS_AUDIT_SIGNING_KEY, so grants are signed with "
                      "an ephemeral process-local key. Set one for anything real.")
            print()

        selected = scenarios()
        if args.scenario:
            wanted = set(args.scenario)
            selected = [s for s in selected if s.invoice_id in wanted]
            unknown = sorted(wanted - {s.invoice_id for s in selected})
            if unknown:
                print(f"no such scenario: {', '.join(unknown)}")
                return 2

        outcomes: list[Outcome] = []
        for scenario in selected:
            if not args.json:
                invoice = app.book.get_invoice(scenario.invoice_id) or {}
                print(f"  {scenario.invoice_id}  {invoice.get('amount', '?'):>10} "
                      f"{invoice.get('currency', '')}  {scenario.task_type}")
                print(f"    control: {scenario.control}")
            outcome = run_scenario(app, scenario, owner)
            outcomes.append(outcome)
            if not args.json:
                print(f"    -> {outcome.status.upper()}"
                      + (f"  ({outcome.detail})" if outcome.detail else ""))
                if args.trace:
                    print(f"       granted: {outcome.granted_tools}")
                    if outcome.withheld_tools:
                        print(f"       withheld: {outcome.withheld_tools}")
                    for a in outcome.actions:
                        verdict = (a.get("verdict") or {})
                        print(f"       {a.get('tool'):26s} "
                              f"{str(verdict.get('decision', 'read-only')):8s} "
                              f"{verdict.get('rule', '')} "
                              f"executed={a.get('executed')}")
                        if verdict.get("reason"):
                            print(f"           {verdict['reason']}")
                print()

        if args.json:
            payload = {
                "app_id": app.app_id,
                "tasks": app.task_types(),
                "scenarios": [o.as_dict() for o in outcomes],
                "owner_decisions": getattr(owner, "decisions", []),
                "audit_log": str(app.audit_log_path),
                "dashboard": app.book.snapshot(),
            }
            print(json.dumps(payload, indent=2, default=str))
            return 0

        print(render_dashboard(app, outcomes))
        print()
        print(render_queue(app, outcomes))
        print()
        print(render_settings(app))
        print()
        print(f"  action logs: {app.integration.runtime.runs_dir}")
        print(f"  audit log (hash-chained): {app.audit_log_path}")
        return 0
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())

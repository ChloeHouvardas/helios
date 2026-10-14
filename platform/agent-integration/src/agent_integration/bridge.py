"""The audit bridge: how an action agent asks permission and obeys.

This is the seam the two builders left open. ``audit-agent-builder``'s README
flags the overlap between its standalone auditor and the action side's
in-process ``AuditGate`` and says the resolution "is an architecture
decision, not an implementation detail". This module implements the
recommended one: **the in-app gate becomes a client of the standalone
auditor.**

So the action agent no longer polices itself. It talks to an auditor that
runs outside it, over any of the auditor's transports, and the auditor's
verdict is the only one that counts. The action side's own ``AuditGate``
remains importable for unit tests but is not in this loop.

Two interactions, per ``specs/interfaces.md`` §3:

1. ``authorize_task()`` — before work starts. The auditor answers *which
   tools may this agent use for this task*, and the runtime offers the model
   **only** those. An ungranted tool is invisible to the model, which is
   stronger than rejecting it after the fact.
2. ``review()`` — before each side effect. Allow, hold or deny.

Grant lifetime is the subtle part. Side-effect grants are single-use
(``max_uses=1``) and short-lived, on the reading that a grant is authority
to act *now*. So the bridge requests a fresh authorization for every
side-effect call rather than reusing one; a spent grant is refused by the
auditor's own replay guard, and silently reusing it would turn every second
email into a deny.

Every method is fail-closed: a transport that cannot be reached yields a
reject with the reason attached, never a default allow.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from agent_toolkit.models import APPROVE, HOLD, REJECT, Proposal, Verdict
from audit_agent.contracts import (
    ActionDecision,
    ActionRequest,
    AuthorizationRequest,
    Decision,
    TaskAuthorization,
)


def _new_request_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class TaskSession:
    """Authority granted for one task, and the tools it covers.

    ``allowed_tools`` is the auditor's answer, not the agent's wish list.
    The runtime narrows the model-facing tool list to exactly this, so a
    tool the auditor withheld is never even offered.
    """

    task_type: str
    task_summary: str
    app_id: str
    agent_id: str
    principal: str
    #: The authorization that established this session, for evidence.
    grant: TaskAuthorization | None
    allowed_tools: tuple[str, ...]
    #: Requested but withheld, kept so the agent can explain itself.
    denied_tools: tuple[str, ...] = ()
    #: True when no usable grant was issued at all.
    authorized: bool = False
    #: Why not, when not authorized.
    reason: str = ""
    #: tool -> side_effect, from the grant's own permissions.
    side_effects: Mapping[str, bool] = field(default_factory=dict)

    def permits(self, tool: str) -> bool:
        return tool in self.allowed_tools

    def is_gated(self, tool: str) -> bool:
        """True when every call to this tool must be adjudicated."""
        if tool in self.side_effects:
            return bool(self.side_effects[tool])
        # Unknown: treat as gated. Assuming read-only would be the one
        # mistake here that lets a side effect through unchecked.
        return True


@dataclass(frozen=True)
class GateResult:
    """The bridge's verdict on one proposal.

    Wraps an action-side ``Verdict`` so the existing runtime models stay
    intact, and carries the three things a hold needs that ``Verdict``
    cannot express: the hold id to resume with, the owner's question, and
    any arguments the auditor clamped.
    """

    verdict: Verdict
    #: The auditor's full decision, when there was one.
    decision: ActionDecision | None = None
    #: Set on hold: resume with this id.
    hold_id: str | None = None
    #: Set on hold: what the owner is being asked.
    owner_question: str = ""
    #: Arguments the auditor granted. When present, the runtime MUST execute
    #: these, never the agent's original request.
    granted_arguments: dict[str, Any] | None = None
    #: Non-binding model/heuristic commentary. Never affects the verdict.
    advisory_notes: tuple[str, ...] = ()
    #: The authorization the call was made under, for the action log.
    authorization_id: str | None = None

    @property
    def approved(self) -> bool:
        return self.verdict.decision == APPROVE

    @property
    def held(self) -> bool:
        return self.verdict.decision == HOLD

    @property
    def rejected(self) -> bool:
        return self.verdict.decision == REJECT

    def arguments_to_execute(self, original: Mapping[str, Any]) -> dict[str, Any]:
        """What actually runs: the granted value, or the original."""
        if self.granted_arguments:
            return dict(self.granted_arguments)
        return dict(original)


_DECISION_TO_VERDICT = {
    Decision.ALLOW: APPROVE,
    Decision.HOLD: HOLD,
    Decision.DENY: REJECT,
}


class AuditBridge:
    """Client-side half of the action/audit seam."""

    def __init__(
        self,
        client: Any,
        *,
        app_id: str,
        agent_id: str,
        principal: str = "owner",
    ) -> None:
        self.client = client
        self.app_id = app_id
        self.agent_id = agent_id
        self.principal = principal
        #: hold_id -> GateResult, so a resumed run can find what it paused on.
        self._holds: dict[str, GateResult] = {}
        #: hold_id -> the exact action that was held (tool + arguments), so a
        #: resume executes precisely what the owner saw, nothing more.
        self._held_actions: dict[str, dict[str, Any]] = {}

    # -- Interaction 1: which tools may I use ------------------------------

    def authorize_task(
        self,
        task_type: str,
        task_summary: str,
        requested_tools: Sequence[str],
    ) -> TaskSession:
        """Ask the auditor which tools this agent may use for this task.

        A transport or policy failure becomes a session with no authority at
        all. The agent then cannot act — which is correct, and is
        distinguishable in the log from a policy denial because the reason
        names the failure.
        """
        request = AuthorizationRequest(
            request_id=_new_request_id("authz"),
            app_id=self.app_id,
            agent_id=self.agent_id,
            principal=self.principal,
            task_type=task_type,
            task_summary=task_summary,
            requested_tools=list(requested_tools),
        )
        try:
            grant = self.client.authorize(request)
        except Exception as exc:  # noqa: BLE001 - fail closed, always
            # Not just TransportError/AuditAgentError: any failure to obtain
            # authority must mean *no* authority. Letting an unexpected
            # exception escape would crash the run and leave the action log
            # without a terminal entry, which reads like an action that was
            # attempted and never adjudicated.
            return TaskSession(
                task_type=task_type,
                task_summary=task_summary,
                app_id=self.app_id,
                agent_id=self.agent_id,
                principal=self.principal,
                grant=None,
                allowed_tools=(),
                denied_tools=tuple(requested_tools),
                authorized=False,
                reason=f"the audit agent could not be reached: {exc}",
            )

        allowed = tuple(grant.tool_names())
        denied = tuple(t for t in requested_tools if t not in allowed)
        side_effects = {p.tool: bool(p.side_effect) for p in grant.permissions}
        authorized = grant.decision is Decision.ALLOW and bool(allowed)

        if not authorized and not grant.findings:
            reason = grant.owner_brief or "the audit agent granted no tools"
        elif not authorized:
            reason = grant.findings[0].message
        else:
            reason = ""

        return TaskSession(
            task_type=task_type,
            task_summary=task_summary,
            app_id=self.app_id,
            agent_id=self.agent_id,
            principal=self.principal,
            grant=grant,
            allowed_tools=allowed,
            denied_tools=denied,
            authorized=authorized,
            reason=reason,
            side_effects=side_effects,
        )

    # -- Interaction 2: may this specific call proceed ---------------------

    def review(
        self, session: TaskSession, proposal: Proposal
    ) -> GateResult:
        """Adjudicate one proposal. Returns a verdict the runtime obeys."""
        # Local pre-check, before spending a round trip. The auditor would
        # deny this anyway; checking here keeps the reason precise and the
        # log honest about who refused.
        if not session.authorized:
            return GateResult(
                verdict=Verdict(
                    decision=REJECT,
                    rule="task_not_authorized",
                    reason=(
                        f"the audit agent did not authorize task "
                        f"'{session.task_type}': {session.reason}"
                    ),
                )
            )
        if not session.permits(proposal.tool):
            return GateResult(
                verdict=Verdict(
                    decision=REJECT,
                    rule="tool_not_granted",
                    reason=(
                        f"tool '{proposal.tool}' was not granted for task "
                        f"'{session.task_type}'; granted: "
                        f"{list(session.allowed_tools)}"
                    ),
                )
            )
        if not session.is_gated(proposal.tool):
            # specs/interfaces.md §3: read-only tool calls run directly.
            return GateResult(
                verdict=Verdict(
                    decision=APPROVE,
                    rule="read_only_direct",
                    reason=(
                        f"'{proposal.tool}' is read-only and runs directly "
                        "without per-call review"
                    ),
                )
            )

        # A side-effect grant is single-use, so ask for a fresh one. Reusing
        # a spent grant is a replay and the auditor refuses it.
        grant = self._fresh_grant(session)
        if grant is None:
            return GateResult(
                verdict=Verdict(
                    decision=REJECT,
                    rule="grant_unavailable",
                    reason=(
                        "the audit agent could not issue authority for this "
                        "call, so it is refused"
                    ),
                )
            )

        request = ActionRequest(
            request_id=proposal.proposal_id,
            app_id=self.app_id,
            agent_id=self.agent_id,
            principal=self.principal,
            task_type=session.task_type,
            tool=proposal.tool,
            arguments=dict(proposal.arguments),
            authorization=grant,
        )
        try:
            decision = self.client.adjudicate(request)
        except Exception as exc:  # noqa: BLE001 - fail closed, always
            # adjudicate() on the file/HTTP transports already converts a
            # transport failure into a deny; this catches the in-process case,
            # any other client, and any unexpected error. Still a deny, never
            # an allow — the side effect does not happen.
            return GateResult(
                verdict=Verdict(
                    decision=REJECT,
                    rule="transport.unavailable",
                    reason=f"the audit agent could not be reached: {exc}",
                )
            )

        verdict = Verdict(
            decision=_DECISION_TO_VERDICT[decision.decision],
            rule=self._primary_rule(decision),
            reason=decision.reason or decision.decision.value,
        )
        result = GateResult(
            verdict=verdict,
            decision=decision,
            hold_id=decision.hold_id,
            owner_question=decision.owner_question or "",
            granted_arguments=decision.granted_arguments,
            advisory_notes=tuple(decision.advisory_notes),
            authorization_id=decision.authorization_id,
        )
        if result.held and decision.hold_id:
            self._holds[decision.hold_id] = result
            # Remember the exact action, so a resume executes precisely what
            # the owner was shown — not a re-proposal the model could widen.
            self._held_actions[decision.hold_id] = {
                "tool": proposal.tool,
                "arguments": dict(proposal.arguments),
                "granted_arguments": decision.granted_arguments,
                "task_type": session.task_type,
                "task_summary": session.task_summary,
            }
        return result

    def _fresh_grant(self, session: TaskSession) -> TaskAuthorization | None:
        """A new single-use grant for one side-effect call."""
        request = AuthorizationRequest(
            request_id=_new_request_id("call"),
            app_id=self.app_id,
            agent_id=self.agent_id,
            principal=self.principal,
            task_type=session.task_type,
            task_summary=session.task_summary,
            requested_tools=list(session.allowed_tools),
        )
        try:
            grant = self.client.authorize(request)
        except Exception:  # noqa: BLE001 - no grant means no action
            return None
        if grant.decision is not Decision.ALLOW:
            return None
        # An allow with no tools is unusable; refuse rather than present it.
        return grant if grant.tool_names() else None

    @staticmethod
    def _primary_rule(decision: ActionDecision) -> str:
        """The rule id worth showing: most severe finding first."""
        if not decision.findings:
            return decision.decision.value
        rank = {"critical": 0, "warn": 1, "info": 2}
        ranked = sorted(
            decision.findings, key=lambda f: rank.get(f.severity.value, 3)
        )
        return ranked[0].rule_id

    # -- Holds -------------------------------------------------------------

    def pending_hold(self, hold_id: str) -> GateResult | None:
        return self._holds.get(hold_id)

    def hold_status(self, hold_id: str) -> Any:
        """The auditor's own record of a hold, including its status.

        Read from the auditor rather than from local state: whether an owner
        approved is the auditor's fact to own, and on the file/HTTP transports
        the decision may have been recorded by a different process.
        """
        getter = getattr(self.client, "get_hold", None)
        if callable(getter):
            return getter(hold_id)
        # In-process transport exposes the agent directly.
        agent = getattr(self.client, "agent", None)
        getter = getattr(agent, "get_hold", None)
        if callable(getter):
            return getter(hold_id)
        for record in self.client.pending_holds():
            if record.hold_id == hold_id:
                return record
        return None

    def held_action(self, hold_id: str) -> dict[str, Any] | None:
        """The exact tool call that was held, or None if it is not remembered.

        A resume executes this and only this. Re-proposing the action to the
        auditor instead would hold again — the fact that triggered the hold
        (a 12500.00 invoice, a 60-day extension) has not changed. What changed
        is that a human now approves it, which is recorded against the hold.
        """
        return self._held_actions.get(hold_id)

    def resolve_hold(
        self, hold_id: str, *, approve: bool, decided_by: str, note: str = ""
    ) -> Any:
        """Record the owner's decision so a held run can resume."""
        record = self.client.resolve_hold(
            hold_id, approve=approve, decided_by=decided_by, note=note
        )
        self._holds.pop(hold_id, None)
        # The action stays remembered: the runtime still needs it to execute
        # what was approved. It is dropped once the resume consumes it.
        return record

    def forget_held_action(self, hold_id: str) -> dict[str, Any] | None:
        """Hand over the held action and stop remembering it (single-use)."""
        return self._held_actions.pop(hold_id, None)


__all__ = ["AuditBridge", "GateResult", "TaskSession"]

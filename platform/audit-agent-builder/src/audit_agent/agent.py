"""The Audit Agent.

A standalone agent that decides what an action agent may do. It answers two
questions and records both:

1. `authorize()` — "for this task, which tools may I use, under what
   limits, for how long?" Returns a signed `TaskAuthorization`.
2. `adjudicate()` — "may I make this specific call right now?" Returns an
   `ActionDecision` of allow, hold or deny.

It is standalone in the strongest useful sense: it shares no code with the
action agent at runtime. The two exchange the JSON models in `contracts.py`
over a transport. It can run in the same process (in-process transport), a
separate process (file transport), or a separate container (HTTP transport)
without the action agent changing.

Design rules it enforces, all traceable to `specs/decisions.md`:

- **Deterministic and fail-closed.** The decision is produced by rules, not
  by a model. An unknown tool, a malformed argument, an unverifiable fact
  or an invalid grant is a deny, never a pass. Advisory model commentary
  (see `advisory.py`) is attached to the decision and can never change it.
- **Model proposes, code decides.** The action agent's claims are inputs to
  a search for facts; the facts come from `FactSource`s it does not control.
- **Least authority, short-lived.** Grants are scoped to one app, agent,
  task and principal, expire quickly, and are single-use for side effects.
- **Everything is evidence.** Every authorization and decision is appended
  to a hash-chained log, with the findings that produced it.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .advisory import AdvisoryReviewer, NoOpReviewer
from .audit_log import AuditEvent, AuditLog, EventType
from .contracts import (
    ActionDecision,
    ActionRequest,
    AuthorizationRequest,
    Decision,
    Finding,
    HoldRecord,
    Precedence,
    Severity,
    TaskAuthorization,
    ToolPermission,
    utcnow,
)
from .errors import (
    AuditAgentError,
    GrantError,
    GrantInvalidError,
    PolicyError,
    RequestError,
    SigningKeyMissingError,
    UnknownTaskTypeError,
    UnknownToolError,
)
from .facts import FactResolver, FactSource
from .policy import (
    AuditFacts,
    GrantDraft,
    InMemoryReplayGuard,
    Keyring,
    PolicyEngine,
    PolicyPack,
    ReplayGuard,
    RuleOutcome,
    ToolDescriptor,
    ToolRegistry,
)


class AuditAgent:
    """Decides and records what an action agent is allowed to do.

    Constructed from a composed `PolicyEngine`, a `FactResolver`, an
    `AuditLog` and a `Keyring`. All four are injectable so tests and
    deployments can supply their own, and so the agent has no hidden
    dependencies on the action agent's code.
    """

    def __init__(
        self,
        engine: PolicyEngine,
        *,
        facts: FactResolver | None = None,
        log: AuditLog | None = None,
        keyring: Keyring | None = None,
        replay_guard: ReplayGuard | None = None,
        reviewer: AdvisoryReviewer | None = None,
        default_ttl_seconds: int = 300,
        now: Any = None,
    ) -> None:
        self.engine = engine
        self.facts = facts or FactResolver(None)
        self.log = log
        self.keyring = keyring or engine.verifier.keyring
        self.replay_guard = replay_guard or InMemoryReplayGuard()
        self.reviewer = reviewer or NoOpReviewer()
        self.default_ttl_seconds = default_ttl_seconds
        self._now = now
        #: Holds awaiting an owner decision, keyed by hold id.
        self._holds: dict[str, HoldRecord] = {}
        #: The manifest this agent was built from, when there was one. Set by
        #: `builder.build_audit_agent`. Typed loosely on purpose: the runtime
        #: must work from a composed PolicyEngine alone, so it does not
        #: depend on the manifest format and can be constructed by hand.
        self.manifest: Any = None

    # -- Construction helpers --------------------------------------------

    @classmethod
    def build(
        cls,
        registry: ToolRegistry,
        *,
        app_packs: Iterable[PolicyPack] = (),
        fact_source: FactSource | None = None,
        log_path: str | Path | None = None,
        keyring: Keyring | None = None,
        reviewer: AdvisoryReviewer | None = None,
        default_ttl_seconds: int = 300,
        contact_window_days: int = 7,
        now: Any = None,
    ) -> "AuditAgent":
        """One-call construction for the common case.

        `keyring` defaults to the environment key. Passing none and having
        no `HELIOS_AUDIT_SIGNING_KEY` set is a configuration error at
        signing time, not a silent fallback to unsigned grants.
        """
        replay_guard = InMemoryReplayGuard()
        keyring = keyring or Keyring.from_env()
        if not keyring.can_sign and not keyring.allow_unsigned:
            # Fail at startup rather than on the first authorize() call. An
            # audit agent that cannot sign cannot make grants anyone should
            # trust, and discovering that mid-workflow is how unsigned
            # grants end up in production.
            raise SigningKeyMissingError(
                f"the audit agent cannot sign grants: no key is set in "
                f"{Keyring.ENV_VAR}. Set it, or construct the Keyring with "
                "allow_unsigned=True for local development only."
            )
        engine = PolicyEngine.compose(
            registry,
            app_packs=app_packs,
            keyring=keyring,
            replay_guard=replay_guard,
            now=now,
        )
        return cls(
            engine,
            facts=FactResolver(fact_source, contact_window_days=contact_window_days),
            log=AuditLog(log_path) if log_path else None,
            keyring=engine.verifier.keyring,
            replay_guard=replay_guard,
            reviewer=reviewer,
            default_ttl_seconds=default_ttl_seconds,
            now=now,
        )

    def _current(self) -> Any:
        return self._now() if callable(self._now) else utcnow()

    # -- Public capabilities ---------------------------------------------

    @property
    def registry(self) -> ToolRegistry:
        return self.engine.composed.registry

    def task_types(self) -> list[str]:
        """Task types this audit agent can authorize."""
        return self.engine.composed.task_types

    def tools_for_task(self, task_type: str) -> list[str]:
        """Which tools a task type may be granted, before narrowing.

        This is the direct, introspectable answer to "what can the action
        agent use for this task" — useful for the action agent to plan and
        for a developer to check a manifest against.
        """
        task = self.engine.composed.task_policy(task_type)
        return list(task.allowed_tools)

    def describe_capabilities(self) -> dict[str, Any]:
        """A machine-readable capability summary.

        An action agent (or a developer, or a judge) can call this to learn
        the full authority surface without reading policy source: every
        task type, the tools each may use, which are gated, and the grant
        lifetime. It is derived from composed policy, so it cannot drift.
        """
        registry = self.registry
        out: dict[str, Any] = {"tasks": {}, "tools": {}}
        for task_type in self.task_types():
            task = self.engine.composed.task_policy(task_type)
            out["tasks"][task_type] = {
                "description": task.description,
                "allowed_tools": list(task.allowed_tools),
                "ttl_seconds": task.ttl_seconds,
            }
        for name in registry.names:
            descriptor = registry.get(name)
            out["tools"][name] = {
                "description": descriptor.description,
                "side_effect": descriptor.side_effect,
                "gated": descriptor.side_effect,
            }
        return out

    # -- Interaction 1: task authorization -------------------------------

    def authorize(self, request: AuthorizationRequest) -> TaskAuthorization:
        """Decide which tools the action agent may use for a task.

        Pipeline, each step able to narrow but never widen:

        1. Draft the grant from policy ∩ requested tools.
        2. Resolve facts for the subjects in scope.
        3. Run authorization-time rules (baseline then app), letting them
           tighten limits, drop tools, or veto the whole task.
        4. Resolve competing findings into one decision.
        5. Sign and record.

        A deny still returns a well-formed authorization carrying the
        findings, so the action agent learns *why* and can adjust — this is
        the feedback loop the platform is built around, not an exception.
        """
        request = self._validate_authorization_request(request)
        if self.log:
            self.log.append(
                EventType.AUTHORIZATION_REQUESTED,
                app_id=request.app_id,
                agent_id=request.agent_id,
                principal=request.principal,
                task_type=request.task_type,
                request_id=request.request_id,
                details={
                    "requested_tools": request.requested_tools,
                    "task_summary": request.task_summary,
                    "context": request.context,
                },
            )

        # 1. Draft from policy. An unknown task type raises, which we turn
        #    into a deny below rather than crashing the caller.
        try:
            draft = self.engine.draft_grant(request.task_type, request.requested_tools)
        except UnknownTaskTypeError as exc:
            return self._refuse_authorization(
                request,
                Finding(
                    rule_id="baseline.task_known",
                    effect="deny",
                    severity=Severity.CRITICAL,
                    message=str(exc),
                    details={"task_type": request.task_type},
                ),
            )
        except UnknownToolError as exc:
            return self._refuse_authorization(
                request,
                Finding(
                    rule_id="baseline.tool_registered",
                    effect="deny",
                    severity=Severity.CRITICAL,
                    message=str(exc),
                ),
            )

        # 2. Facts for every subject named in context/evidence. Authorization
        #    is about the task, so subjects come from context, not call args.
        facts = self._facts_for_authorization(request)

        # 3. Rules. Baseline first, then app; each may tighten the draft.
        findings: list[Finding] = list(draft.findings)
        overrides: dict[str, Any] = {}
        for rule in self.engine.composed.rules_for(request.task_type):
            handler = getattr(rule, "at_authorization", None)
            if not callable(handler):
                continue
            try:
                outcome = handler(request, facts, draft)
            except AuditAgentError as exc:
                outcome = RuleOutcome(
                    effect="deny",
                    message=str(exc),
                    severity=Severity.CRITICAL,
                )
            except Exception as exc:  # a buggy rule fails closed
                outcome = RuleOutcome(
                    effect="deny",
                    message=f"rule '{rule.rule_id}' failed: {exc}",
                    severity=Severity.CRITICAL,
                )
            if outcome is None or not outcome.fired:
                continue
            assert outcome.effect is not None
            source = getattr(rule, "source", "app")
            findings.append(outcome.as_finding(rule.rule_id, source))
            overrides.update(outcome.argument_overrides)

        # 4. Resolve. The decision reflects the most restrictive finding.
        decision = Precedence.resolve(findings)

        # A deny at authorization time means no usable grant: strip
        # permissions so even a mishandled authorization cannot be acted on.
        permissions = (
            list(draft.permissions.values())
            if decision is Decision.ALLOW
            else []
        )

        ttl = self._ttl_for(request.task_type)
        authorization = TaskAuthorization(
            authorization_id=f"az_{uuid.uuid4().hex[:16]}",
            request_id=request.request_id,
            app_id=request.app_id,
            agent_id=request.agent_id,
            principal=request.principal,
            task_type=request.task_type,
            decision=decision,
            permissions=permissions,
            findings=findings,
            issued_at=self._current(),
            expires_at=self._current() + timedelta(seconds=ttl),
            max_uses=self._max_uses_for(request.task_type),
            owner_brief=self._owner_brief(request, permissions, findings),
        )

        # 5. Sign and record. Signing is where a missing key surfaces.
        authorization = self.keyring.sign(authorization)
        if self.log:
            self.log.record_authorization(request.request_id, authorization)

        # Advisory commentary is produced AFTER the decision is signed, and
        # is logged separately so the evidence trail never mixes a
        # non-binding opinion into the grant itself.
        advisory = self.reviewer.review_authorization(request, authorization)
        if advisory and self.log:
            self.log.append(
                EventType.ADVISORY,
                app_id=request.app_id,
                agent_id=request.agent_id,
                principal=request.principal,
                task_type=request.task_type,
                request_id=request.request_id,
                authorization_id=authorization.authorization_id,
                decision=authorization.decision,
                details={"notes": advisory},
            )
        return authorization

    # -- Interaction 2: action adjudication ------------------------------

    def adjudicate(self, request: ActionRequest) -> ActionDecision:
        """Decide whether one concrete side-effect call may proceed.

        Verifies the presented grant, resolves facts from trusted sources,
        runs action-time rules, resolves the decision, applies any clamped
        arguments, and records the outcome. A hold creates a resumable
        `HoldRecord`.
        """
        request = self._validate_action_request(request)
        authorization = request.authorization
        permission = (
            authorization.permission_for(request.tool) if authorization else None
        )

        # Facts for this specific call: subjects come from the arguments.
        facts = self._facts_for_action(request, authorization)

        findings: list[Finding] = []
        overrides: dict[str, Any] = {}
        for rule in self.engine.composed.rules_for(request.task_type):
            handler = getattr(rule, "at_action", None)
            if not callable(handler):
                continue
            try:
                outcome = handler(request, facts, permission, authorization)
            except AuditAgentError as exc:
                outcome = RuleOutcome(
                    effect="deny", message=str(exc), severity=Severity.CRITICAL
                )
            except Exception as exc:  # a buggy rule fails closed
                outcome = RuleOutcome(
                    effect="deny",
                    message=f"rule '{rule.rule_id}' failed: {exc}",
                    severity=Severity.CRITICAL,
                )
            if outcome is None or not outcome.fired:
                continue
            assert outcome.effect is not None
            source = getattr(rule, "source", "app")
            findings.append(outcome.as_finding(rule.rule_id, source))
            overrides.update(outcome.argument_overrides)

        decision = Precedence.resolve(findings)

        # A grant that permits the call is consumed, so it cannot be
        # replayed. Consumed only on allow: a deny or hold must leave the
        # grant usable for a corrected retry.
        if decision is Decision.ALLOW and authorization is not None:
            self.replay_guard.consume(
                authorization.authorization_id, request.request_id
            )

        granted_arguments = self._apply_overrides(request.arguments, overrides)
        advisory = self.reviewer.review_action(request, decision, findings)

        action_decision = ActionDecision(
            request_id=request.request_id,
            authorization_id=(
                authorization.authorization_id if authorization else None
            ),
            app_id=request.app_id,
            agent_id=request.agent_id,
            tool=request.tool,
            task_type=request.task_type,
            decision=decision,
            findings=findings,
            decided_at=self._current(),
            granted_arguments=granted_arguments if overrides else None,
            reason=self._reason(decision, findings),
            advisory_notes=advisory,
        )

        if decision is Decision.HOLD:
            hold = self._create_hold(request, action_decision)
            action_decision = action_decision.model_copy(
                update={
                    "hold_id": hold.hold_id,
                    "owner_question": hold.owner_question,
                }
            )

        if self.log:
            self.log.record_decision(request.request_id, action_decision)
            if action_decision.advisory_notes:
                self.log.append(
                    EventType.ADVISORY,
                    app_id=request.app_id,
                    agent_id=request.agent_id,
                    principal=request.principal,
                    task_type=request.task_type,
                    tool=request.tool,
                    request_id=request.request_id,
                    authorization_id=action_decision.authorization_id or "",
                    decision=action_decision.decision,
                    details={"notes": action_decision.advisory_notes},
                )
        return action_decision

    # -- Holds -----------------------------------------------------------

    def _create_hold(
        self, request: ActionRequest, decision: ActionDecision
    ) -> HoldRecord:
        hold = HoldRecord(
            hold_id=f"hold_{uuid.uuid4().hex[:16]}",
            request_id=request.request_id,
            app_id=request.app_id,
            agent_id=request.agent_id,
            principal=request.principal,
            task_type=request.task_type,
            tool=request.tool,
            arguments=request.arguments,
            findings=decision.findings,
            owner_question=self._owner_question(request, decision.findings),
            created_at=self._current(),
        )
        self._holds[hold.hold_id] = hold
        if self.log:
            self.log.append(
                EventType.HOLD_CREATED,
                app_id=hold.app_id,
                agent_id=hold.agent_id,
                principal=hold.principal,
                task_type=hold.task_type,
                tool=hold.tool,
                request_id=hold.request_id,
                authorization_id=decision.authorization_id or "",
                hold_id=hold.hold_id,
                decision=Decision.HOLD,
                findings=hold.findings,
                details={"owner_question": hold.owner_question},
            )
        return hold

    def pending_holds(self, principal: str | None = None) -> list[HoldRecord]:
        """Holds awaiting a decision, optionally filtered to one owner."""
        holds = [h for h in self._holds.values() if h.status == "pending"]
        if principal is not None:
            holds = [h for h in holds if h.principal == principal]
        return sorted(holds, key=lambda h: h.created_at)

    def get_hold(self, hold_id: str) -> HoldRecord | None:
        return self._holds.get(hold_id)

    def resolve_hold(
        self,
        hold_id: str,
        *,
        approve: bool,
        decided_by: str,
        note: str = "",
    ) -> HoldRecord:
        """Record an owner's decision on a hold.

        The decision applies to the exact action that was held — the
        original arguments are preserved on the record so the runtime can
        compare what it is about to execute against what the owner saw. An
        approval here authorizes *that* action, not a class of actions.
        """
        hold = self._holds.get(hold_id)
        if hold is None:
            raise GrantInvalidError(f"no hold with id '{hold_id}'")
        if hold.status != "pending":
            raise GrantInvalidError(
                f"hold '{hold_id}' was already {hold.status}; a decision applies once"
            )
        hold = hold.model_copy(
            update={
                "status": "approved" if approve else "rejected",
                "decided_by": decided_by,
                "decided_at": self._current(),
            }
        )
        self._holds[hold_id] = hold
        if self.log:
            self.log.append(
                EventType.HOLD_RESOLVED,
                app_id=hold.app_id,
                agent_id=hold.agent_id,
                principal=hold.principal,
                task_type=hold.task_type,
                tool=hold.tool,
                request_id=hold.request_id,
                hold_id=hold.hold_id,
                decision=Decision.ALLOW if approve else Decision.DENY,
                details={"decided_by": decided_by, "note": note},
            )
        return hold

    # -- Internal helpers -------------------------------------------------

    def _validate_authorization_request(
        self, request: AuthorizationRequest
    ) -> AuthorizationRequest:
        """Reject structurally invalid requests before any decision logic."""
        if not request.app_id or not request.agent_id:
            raise RequestError("app_id and agent_id are required")
        if not request.task_type:
            raise RequestError("task_type is required")
        return request

    def _validate_action_request(self, request: ActionRequest) -> ActionRequest:
        if not request.app_id or not request.agent_id:
            raise RequestError("app_id and agent_id are required")
        if not request.tool:
            raise RequestError("tool is required")
        if not request.task_type:
            raise RequestError("task_type is required")
        return request

    def _facts_for_authorization(
        self, request: AuthorizationRequest
    ) -> AuditFacts:
        """Resolve facts for the subjects a request names.

        Only subject identifiers are taken from the request. Everything else
        in `context` and `evidence` is a claim the agent made about the
        world, and claims are not facts: an agent that could seed
        `amount_due` here could satisfy any amount limit it wanted. The
        resolver looks each named subject up in the fact source instead.

        Values the agent supplies are still *logged*, because what an agent
        believed is useful evidence. They simply never enter the facts a
        rule reads.
        """
        merged: dict[str, Any] = {}
        merged.update(request.context)
        merged.update(request.evidence)
        return self.facts.resolve(merged)

    def _facts_for_action(
        self,
        request: ActionRequest,
        authorization: TaskAuthorization | None,
    ) -> AuditFacts:
        """Resolve facts for one call, including the tool's usage count.

        `request.resolved_facts` is deliberately NOT merged into the result.
        It arrives from the action agent's side of the wire, so treating it
        as trusted would let the agent assert the very values a limit is
        meant to check — the amount it is paying, whether the payee's bank
        details changed, whether the invoice is a duplicate. It is used only
        to name additional subjects worth looking up.

        The one non-request input here is the call count, which comes from
        this agent's own replay guard and is therefore trusted.
        """
        counts: dict[str, int] = {}
        if authorization is not None and self.replay_guard is not None:
            uses = getattr(self.replay_guard, "uses", None)
            if callable(uses):
                counts[request.tool] = uses(authorization.authorization_id)
        # Subject ids mentioned in resolved_facts are worth resolving, even
        # though their values are not believed.
        extra_subjects = [
            value
            for key, value in request.resolved_facts.items()
            if key in self.facts.SUBJECT_KEYS and isinstance(value, str) and value
        ]
        return self.facts.resolve(
            request.arguments,
            extra_subjects=extra_subjects,
            tool_call_counts=counts,
        )

    def _ttl_for(self, task_type: str) -> int:
        try:
            return self.engine.composed.task_policy(task_type).ttl_seconds
        except UnknownTaskTypeError:
            return self.default_ttl_seconds

    def _max_uses_for(self, task_type: str) -> int:
        """Side-effect grants are single-use unless policy says otherwise.

        Derived from the task's own tool overrides: if any allowed
        side-effect tool declares `max_calls`, the grant may be used up to
        the largest such budget; otherwise one use.
        """
        try:
            task = self.engine.composed.task_policy(task_type)
        except UnknownTaskTypeError:
            return 1
        budgets = [
            override.max_calls
            for tool, override in task.tool_overrides.items()
            if self.registry.has(tool)
            and self.registry.side_effect(tool)
            and override.max_calls
        ]
        return max(budgets) if budgets else 1

    def _apply_overrides(
        self, arguments: dict[str, Any], overrides: dict[str, Any]
    ) -> dict[str, Any]:
        """Return arguments with rule clamps applied.

        Only ever lowers a value toward a limit, mirroring Build 1's
        `extension_days_granted`: what executes is the granted value, never
        the agent's original request.
        """
        if not overrides:
            return dict(arguments)
        merged = dict(arguments)
        for key, value in overrides.items():
            merged[key] = value
        return merged

    def _reason(self, decision: Decision, findings: list[Finding]) -> str:
        """One-sentence explanation the action agent can act on."""
        if not findings:
            return {
                Decision.ALLOW: "no policy objected to this action",
                Decision.DENY: "no policy authorised this action",
                Decision.HOLD: "held for owner review",
            }[decision]
        # Prefer the most severe finding's message as the headline reason.
        ranked = sorted(
            findings,
            key=lambda f: {"critical": 0, "warn": 1, "info": 2}[f.severity.value],
        )
        return ranked[0].message

    def _owner_brief(
        self,
        request: AuthorizationRequest,
        permissions: list[ToolPermission],
        findings: list[Finding],
    ) -> str:
        """Plain-language summary of a grant, for the approval queue."""
        tools = ", ".join(p.tool for p in permissions) or "(none)"
        gated = ", ".join(p.tool for p in permissions if p.side_effect) or "(none)"
        lines = [
            f"Task: {request.task_type}",
            f"Intent: {request.task_summary or '(not stated)'}",
            f"Tools granted: {tools}",
            f"Gated (need per-call review): {gated}",
        ]
        notable = [f for f in findings if f.severity is not Severity.INFO]
        if notable:
            lines.append("Notes:")
            lines.extend(f"- {f.message}" for f in notable[:5])
        return "\n".join(lines)[:4000]

    def _owner_question(
        self, request: ActionRequest, findings: list[Finding]
    ) -> str:
        """What the owner must decide, in plain language.

        Phrased for a person who has to make a fast call on a payment or an
        email: what the agent wants to do, then why the audit agent did not
        decide it alone.
        """
        headline = self._reason(Decision.HOLD, findings)
        args = ", ".join(f"{k}={v}" for k, v in sorted(request.arguments.items()))
        return (
            f"An agent wants to call '{request.tool}' ({args}) for task "
            f"'{request.task_type}'. The audit agent held it because: "
            f"{headline}. Approve this exact action?"
        )[:4000]

    def _refuse_authorization(
        self, request: AuthorizationRequest, finding: Finding
    ) -> TaskAuthorization:
        """A structurally-impossible task still yields a signed, logged deny."""
        authorization = TaskAuthorization(
            authorization_id=f"az_{uuid.uuid4().hex[:16]}",
            request_id=request.request_id,
            app_id=request.app_id,
            agent_id=request.agent_id,
            principal=request.principal,
            task_type=request.task_type,
            decision=Decision.DENY,
            permissions=[],
            findings=[finding],
            issued_at=self._current(),
            expires_at=self._current() + timedelta(seconds=self.default_ttl_seconds),
            owner_brief=self._owner_brief(request, [], [finding]),
        )
        authorization = self.keyring.sign(authorization)
        if self.log:
            self.log.record_authorization(request.request_id, authorization)
        return authorization


__all__ = ["AuditAgent"]

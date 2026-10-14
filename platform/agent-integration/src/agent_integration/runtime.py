"""The gated agent loop: propose, review, execute, record.

Build 1 specified this loop but the action side ships no runtime for it
(``pyproject.toml`` names an ``agent_toolkit.cli:main`` that does not
exist). This is that loop, with the audit bridge in the path of every side
effect.

The order of operations is the security property, so it is worth stating
exactly:

1. The auditor is asked which tools this agent may use for this task.
2. The model is offered **only** those tools. An ungranted tool is not
   rejected after the fact — it is never in the prompt.
3. Read-only calls execute directly (``specs/interfaces.md`` §3).
4. Every side-effect call becomes a ``Proposal`` and is adjudicated *before*
   anything happens. On deny, the reason goes back to the model so it can
   adjust. On hold, the run stops and can be resumed. On allow, the auditor's
   ``granted_arguments`` execute — never the model's originals, so a clamped
   value cannot be bypassed.
5. Every step is appended to the action log.

The model is behind a ``Driver`` protocol. ``ScriptedDriver`` makes the whole
loop testable and demoable offline; ``ModelDriver`` (in ``model_driver``) is
the real thing. Nothing else in here knows which is loaded, and no HTTP client
or model SDK is imported unless one is asked for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from agent_toolkit.action_log import LOG_FILENAME, ActionLog
from agent_toolkit.contracts import ToolDefinition, ToolRegistry
from agent_toolkit.models import Proposal, Verdict
from agent_toolkit.store import Store

from .adapters import HeliosStoreFactSource
from .bridge import AuditBridge, GateResult, TaskSession

#: Statuses a finished run can have.
COMPLETED = "completed"
HELD = "held"
ERROR = "error"
REFUSED = "refused"  # the auditor granted no authority for the task


@dataclass(frozen=True)
class ToolCall:
    """One tool call a driver wants to make."""

    name: str
    arguments: dict[str, Any]


class Driver(Protocol):
    """The model backend: given a task and history, propose tool calls."""

    def next_calls(
        self,
        *,
        task: str,
        instructions: str,
        tools: Sequence[ToolDefinition],
        history: Sequence[Mapping[str, Any]],
    ) -> list[ToolCall] | str:
        """Return tool calls to make, or a final text answer."""
        ...


class ScriptedDriver:
    """A fixed sequence of tool calls, for offline runs and tests.

    Deliberately dumb: it ignores history and replays its script. That is
    the point — it proves the *gate* works, independent of what any model
    does. A script can include calls that should be denied, which is how the
    denial path gets tested without a model deciding to misbehave.
    """

    def __init__(self, calls: Sequence[ToolCall], final_text: str = "") -> None:
        self._calls = list(calls)
        self._index = 0
        self.final_text = final_text

    def next_calls(
        self,
        *,
        task: str,
        instructions: str,
        tools: Sequence[ToolDefinition],
        history: Sequence[Mapping[str, Any]],
    ) -> list[ToolCall] | str:
        if self._index >= len(self._calls):
            return self.final_text or "done"
        call = self._calls[self._index]
        self._index += 1
        return [call]


@dataclass
class RunResult:
    """What a run did, and how it ended."""

    run_id: str
    status: str
    #: The task session, when one was established.
    session: TaskSession | None = None
    #: Every recorded call: side effects AND read-only calls. A side-effect
    #: entry carries a `proposal_id` and a `verdict`; a read-only entry has
    #: both as None (it is never adjudicated — see specs/interfaces.md §3).
    #: Filter with `side_effects` when you mean "the gated calls".
    actions: list[dict[str, Any]] = field(default_factory=list)
    #: Text the agent finished with, if any.
    final_text: str = ""
    #: Holds that stopped the run, resumable by id.
    holds: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    #: Where this run's action log was written.
    action_log_path: str = ""

    @property
    def side_effects(self) -> list[dict[str, Any]]:
        """Only the adjudicated calls.

        Read-only calls are appended to `actions` too, so `actions` cannot be
        used to count side effects. A proposal_id is the discriminator: it is
        set exactly when a call was turned into a Proposal and sent to the
        auditor, which is the definition of a side effect here.
        """
        return [a for a in self.actions if a.get("proposal_id") is not None]

    @property
    def executed(self) -> list[dict[str, Any]]:
        """Side effects that actually ran.

        Restricted to side effects: a read-only call has `executed: True` on
        success, but it is not a side effect and must not be counted as one.
        """
        return [a for a in self.side_effects if a.get("executed")]

    def summary(self) -> dict[str, Any]:
        side_effects = self.side_effects
        return {
            "run_id": self.run_id,
            "status": self.status,
            "authorized": bool(self.session and self.session.authorized),
            "granted_tools": list(self.session.allowed_tools) if self.session else [],
            "side_effects_attempted": len(side_effects),
            "side_effects_executed": len(self.executed),
            "holds": [h["hold_id"] for h in self.holds],
            "action_log_path": self.action_log_path,
            "detail": self.detail,
        }


class GatedAgentRuntime:
    """One agent: its tools, its auditor, its log."""

    def __init__(
        self,
        *,
        bridge: AuditBridge,
        tools: Sequence[ToolDefinition],
        store: Store,
        fact_source: HeliosStoreFactSource,
        runs_dir: Path,
        agent_name: str,
        instructions: str = "",
        max_turns: int = 12,
    ) -> None:
        self.bridge = bridge
        self.store = store
        self.fact_source = fact_source
        self.runs_dir = Path(runs_dir)
        self.agent_name = agent_name
        self.instructions = instructions
        self.max_turns = max_turns
        self.registry = ToolRegistry(tools)

    def _tools_named(self, names: Sequence[str]) -> list[ToolDefinition]:
        """The live tools for a set of granted names, in grant order."""
        return [self.registry.require(n) for n in names if n in self.registry]

    def run(
        self,
        *,
        driver: Driver,
        task: str,
        task_type: str,
        run_id: str | None = None,
        requested_tools: Sequence[str] | None = None,
    ) -> RunResult:
        """Run one task under the auditor's authority."""
        import uuid

        run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
        run_dir = self.runs_dir / run_id
        log = ActionLog(run_dir, run_id=run_id, agent=self.agent_name)
        log_path = str(run_dir / LOG_FILENAME)
        requested = list(requested_tools or self.registry.names())

        log.append("run_started", task=task, task_type=task_type,
                   requested_tools=requested)

        # 1. What may this agent use for this task?
        session = self.bridge.authorize_task(task_type, task, requested)
        log.append(
            "authorization",
            task_type=task_type,
            authorized=session.authorized,
            granted_tools=list(session.allowed_tools),
            denied_tools=list(session.denied_tools),
            reason=session.reason,
            authorization_id=(
                session.grant.authorization_id if session.grant else None
            ),
        )
        if not session.authorized:
            log.append("run_finished", status=REFUSED, detail=session.reason)
            return RunResult(
                run_id=run_id, status=REFUSED, session=session,
                detail=session.reason, action_log_path=log_path,
            )

        # 2. Offer the model only what was granted.
        available = self._tools_named(session.allowed_tools)
        history: list[dict[str, Any]] = [{"role": "user", "content": task}]
        result = RunResult(
            run_id=run_id,
            status=COMPLETED,
            session=session,
            action_log_path=log_path,
        )

        for turn in range(self.max_turns):
            try:
                proposal_batch = driver.next_calls(
                    task=task,
                    instructions=self.instructions,
                    tools=available,
                    history=history,
                )
            except Exception as exc:  # a broken model is an error, not an allow
                log.append("error", message=f"driver failed: {exc}")
                log.append("run_finished", status=ERROR, detail=str(exc))
                result.status = ERROR
                result.detail = f"driver failed: {exc}"
                return result

            if isinstance(proposal_batch, str):
                result.final_text = proposal_batch
                history.append({"role": "assistant", "content": proposal_batch})
                break
            if not proposal_batch:
                break

            held = False
            for call in proposal_batch:
                log.append(
                    "tool_call", tool=call.name, arguments=dict(call.arguments),
                    side_effect=self._side_effect(call.name),
                )
                if not self._side_effect(call.name):
                    outcome = self._execute_direct(call, log)
                    result.actions.append(
                        {
                            "proposal_id": None,
                            "tool": call.name,
                            "arguments": dict(call.arguments),
                            # Read-only calls are not adjudicated, per
                            # specs/interfaces.md §3.
                            "verdict": None,
                            "executed": outcome["ok"],
                            "granted_arguments": None,
                            "hold_id": None,
                            "output": outcome["text"],
                        }
                    )
                    if not outcome["ok"]:
                        result.status = ERROR
                        result.detail = outcome["text"]
                        log.append(
                            "run_finished", status=ERROR, detail=outcome["text"]
                        )
                        return result
                    history.append(
                        {"role": "tool", "name": call.name, "content": outcome["text"]}
                    )
                    continue

                # 3-4. Side effect: propose, adjudicate, then act or not.
                proposal = Proposal.create(
                    self.agent_name, call.name, call.arguments
                )
                log.append(
                    "proposal",
                    proposal_id=proposal.proposal_id,
                    tool=proposal.tool,
                    arguments=dict(proposal.arguments),
                )
                gate = self.bridge.review(session, proposal)
                log.append(
                    "verdict",
                    proposal_id=proposal.proposal_id,
                    decision=gate.verdict.decision,
                    rule=gate.verdict.rule,
                    reason=gate.verdict.reason,
                    advisory_notes=list(gate.advisory_notes),
                    authorization_id=gate.authorization_id,
                )
                action_record = {
                    "proposal_id": proposal.proposal_id,
                    "tool": call.name,
                    "arguments": dict(call.arguments),
                    "verdict": gate.verdict.to_dict(),
                    "executed": False,
                    "granted_arguments": gate.granted_arguments,
                    "hold_id": gate.hold_id,
                    "output": None,
                }
                result.actions.append(action_record)

                if gate.approved:
                    outcome = self._execute(
                        call.name,
                        gate.arguments_to_execute(call.arguments),
                        log,
                        proposal_id=proposal.proposal_id,
                    )
                    action_record["executed"] = outcome["ok"]
                    action_record["output"] = outcome["text"]
                    if not outcome["ok"]:
                        result.status = ERROR
                        result.detail = outcome["text"]
                        log.append(
                            "run_finished", status=ERROR, detail=outcome["text"]
                        )
                        return result
                    history.append(
                        {"role": "tool", "name": call.name, "content": outcome["text"]}
                    )
                elif gate.held:
                    log.append(
                        "hold",
                        proposal_id=proposal.proposal_id,
                        rule=gate.verdict.rule,
                        reason=gate.verdict.reason,
                        hold_id=gate.hold_id,
                        owner_question=gate.owner_question,
                    )
                    result.holds.append(
                        {
                            "hold_id": gate.hold_id or "",
                            "proposal_id": proposal.proposal_id,
                            "tool": call.name,
                            "arguments": dict(call.arguments),
                            "reason": gate.verdict.reason,
                            "owner_question": gate.owner_question,
                        }
                    )
                    history.append(
                        {
                            "role": "tool",
                            "name": call.name,
                            "content": (
                                f"HELD for owner approval: {gate.verdict.reason}. "
                                "Do not retry this call; the run is paused."
                            ),
                        }
                    )
                    held = True
                else:
                    # The reason goes back to the model. This is the feedback
                    # loop build1.md asks for: "rejected, and the agent adjusts".
                    history.append(
                        {
                            "role": "tool",
                            "name": call.name,
                            "content": (
                                f"DENIED by the audit agent ({gate.verdict.rule}): "
                                f"{gate.verdict.reason}"
                            ),
                        }
                    )
            if held:
                result.status = HELD
                result.detail = (
                    "paused for an owner decision; resume with the hold id"
                )
                result.final_text = "The action is awaiting an owner decision."
                break
        else:
            # The for-loop exhausted max_turns without the model finishing.
            # Reporting this as a success would hide an agent stuck in a loop.
            result.status = ERROR
            result.detail = f"exceeded the {self.max_turns}-turn cap"
            result.final_text = "Stopped at the step cap."
            log.append(
                "step_cap", max_turns=self.max_turns,
                reason="the model kept calling tools without finishing",
            )

        log.append("run_finished", status=result.status, detail=result.detail)
        return result

    def resume(
        self,
        *,
        driver: Driver,
        run_id: str,
        hold_id: str,
        decision: bool | None = None,
        decided_by: str = "owner",
        task: str = "",
        task_type: str = "",
    ) -> RunResult:
        """Continue a run that stopped on a hold.

        The owner's decision applies to the exact action that was held
        (``audit_agent.resolve_hold`` preserves the original arguments for
        precisely this reason), so the runtime does **not** re-propose it to
        the auditor. Re-adjudicating would hold again — the fact that
        triggered the hold, such as a 12500.00 invoice, has not changed. What
        changed is that a human approved it.

        ``decision`` is only used when the hold has not already been resolved
        through the bridge; passing it here is the convenience path.
        """
        run_dir = self.runs_dir / run_id
        log = ActionLog(run_dir, run_id=run_id, agent=self.agent_name)
        held = self.bridge.held_action(hold_id)
        if held is None:
            log.append(
                "resume_refused",
                hold_id=hold_id,
                reason="no held action is remembered for this hold id",
            )
            return RunResult(
                run_id=run_id,
                status=ERROR,
                detail=f"hold '{hold_id}' is unknown or was already resumed",
                action_log_path=str(run_dir / LOG_FILENAME),
            )

        if decision is not None:
            self.bridge.resolve_hold(
                hold_id, approve=decision, decided_by=decided_by,
                note="decided via runtime.resume",
            )
        record = self.bridge.hold_status(hold_id)
        approved = bool(record and getattr(record, "status", "") == "approved")

        log.append(
            "resumed",
            hold_id=hold_id,
            approved=approved,
            tool=held["tool"],
            arguments=held["arguments"],
        )

        result = RunResult(
            run_id=run_id,
            status=COMPLETED,
            action_log_path=str(run_dir / LOG_FILENAME),
        )

        if not approved:
            # The owner said no. The action is not executed, and the reason
            # goes back to the model so it can close the task gracefully.
            denial = (
                f"The owner rejected the held '{held['tool']}' action "
                f"(hold {hold_id}). Do not retry it. Finish the task without "
                "that action and say what you did instead."
            )
            result.status = REFUSED
            result.detail = denial
            result.actions.append(
                {
                    "proposal_id": hold_id,
                    "tool": held["tool"],
                    "arguments": dict(held["arguments"]),
                    "verdict": {
                        "decision": "reject",
                        "rule": "owner.rejected_hold",
                        "reason": denial,
                    },
                    "executed": False,
                    "granted_arguments": None,
                    "hold_id": hold_id,
                    "output": None,
                }
            )
            history: list[dict[str, Any]] = [
                {"role": "user", "content": task or held.get("task_summary", "")},
                {"role": "user", "content": denial},
            ]
            try:
                final = driver.next_calls(
                    task=task or held.get("task_summary", ""),
                    instructions=self.instructions,
                    tools=self._tools_named(self.registry.names()),
                    history=history,
                )
            except Exception as exc:  # a broken model must not become an allow
                log.append("error", message=f"driver failed on resume: {exc}")
                result.status = ERROR
                result.detail = f"driver failed on resume: {exc}"
            else:
                result.final_text = final if isinstance(final, str) else ""
            self.bridge.forget_held_action(hold_id)
            log.append("run_finished", status=result.status, detail=result.detail)
            return result

        # Approved: execute exactly the held action, with any clamping the
        # auditor applied, then hand control back to the model to finish.
        arguments = held.get("granted_arguments") or held["arguments"]
        outcome = self._execute(
            held["tool"],
            dict(arguments),
            log,
            proposal_id=hold_id,
            authorized_by=f"owner:{decided_by}",
        )
        result.actions.append(
            {
                "proposal_id": hold_id,
                "tool": held["tool"],
                "arguments": dict(held["arguments"]),
                "verdict": {
                    "decision": "approve",
                    "rule": "owner.approved_hold",
                    "reason": f"the owner approved this held action ({hold_id})",
                },
                "executed": outcome["ok"],
                "granted_arguments": dict(arguments),
                "hold_id": hold_id,
                "output": outcome["text"],
            }
        )
        if not outcome["ok"]:
            result.status = ERROR
            result.detail = outcome["text"]
            self.bridge.forget_held_action(hold_id)
            log.append("run_finished", status=ERROR, detail=outcome["text"])
            return result

        self.bridge.forget_held_action(hold_id)

        # Let the model close out the task with the real outcome in hand.
        closing = [
            {"role": "user", "content": task or held.get("task_summary", "")},
            {
                "role": "tool",
                "name": held["tool"],
                "content": outcome["text"],
            },
        ]
        try:
            final = driver.next_calls(
                task=task or held.get("task_summary", ""),
                instructions=self.instructions,
                tools=self._tools_named(self.registry.names()),
                history=closing,
            )
        except Exception as exc:
            # The side effect already happened; a chatty model failing on the
            # way out must not make the run look like it never executed.
            log.append("error", message=f"driver failed after resume: {exc}")
            result.status = COMPLETED
            result.final_text = ""
        else:
            if isinstance(final, str):
                result.final_text = final
            # A tool call here would be a *new* side effect on a spent hold.
            # It is deliberately not executed; the run ends instead.
            elif final:
                log.append(
                    "post_resume_calls_ignored",
                    tools=[call.name for call in final],
                    reason=(
                        "a resumed hold authorises one action only; further "
                        "calls need their own authorization"
                    ),
                )

        log.append("run_finished", status=result.status, detail=result.detail)
        return result

    # -- Execution ---------------------------------------------------------

    def _side_effect(self, name: str) -> bool:
        tool = self.registry.get(name)
        return bool(tool and tool.side_effect)

    def _execute_direct(self, call: ToolCall, log: ActionLog) -> dict[str, Any]:
        """Run a read-only call. No proposal, per specs/interfaces.md §3."""
        return self._execute(call.name, dict(call.arguments), log)

    def _execute(
        self,
        name: str,
        arguments: dict[str, Any],
        log: ActionLog,
        proposal_id: str | None = None,
        authorized_by: str | None = None,
    ) -> dict[str, Any]:
        """Run one tool and record the outcome. Never raises."""
        tool = self.registry.get(name)
        if tool is None:
            log.append(
                "action_result", tool=name, arguments=arguments, executed=False,
                reason=f"unknown tool: {name}", proposal_id=proposal_id,
            )
            return {"ok": False, "text": f"unknown tool: {name}"}
        try:
            raw = tool.handler(arguments)
        except Exception as exc:
            log.append(
                "action_result", tool=name, arguments=arguments, executed=False,
                error=f"{type(exc).__name__}: {exc}", proposal_id=proposal_id,
                authorized_by=authorized_by,
            )
            return {"ok": False, "text": f"{name} failed: {exc}"}

        result = raw if isinstance(raw, dict) else {"result": raw}
        # The contact happened. Tell the fact source AFTER execution, so the
        # next frequency check sees a count that reflects reality.
        if name == "send_email":
            customer_id = arguments.get("customer_id")
            if isinstance(customer_id, str):
                self.fact_source.record_contact(customer_id, self.store.today)

        log.append(
            "action_result", tool=name, arguments=arguments, executed=True,
            result=result, proposal_id=proposal_id, authorized_by=authorized_by,
        )
        return {"ok": True, "text": json.dumps(result, ensure_ascii=False, default=str)}


__all__ = [
    "COMPLETED",
    "Driver",
    "ERROR",
    "GateResult",
    "GatedAgentRuntime",
    "HELD",
    "REFUSED",
    "RunResult",
    "ScriptedDriver",
    "ToolCall",
]

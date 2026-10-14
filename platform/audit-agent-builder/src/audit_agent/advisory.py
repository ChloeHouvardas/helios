"""Advisory review: model commentary that cannot change a verdict.

`specs/decisions.md` is explicit that audit decisions are deterministic and
not LLM-produced. That is load-bearing — a financial control that changes
its mind under a different prompt is not a control. But a model reading the
same evidence can still be *useful*: it can flag that a customer email
reads as a hardship case, or that a sequence of actions looks like an
agent working around a limit.

So the reviewer sits beside the rules, not in front of them:

- It runs after the decision is resolved and receives the decision as
  input, not the other way round.
- Its output is a list of strings attached as `advisory_notes` and logged
  as an `ADVISORY` event. No code path reads it to compute a verdict.
- Any failure — no model configured, no API key, a timeout, a malformed
  response — yields no notes. The decision stands unchanged.

That last point is why `qwen-agent` is an optional extra: the audit agent
is fully functional with zero model access, which also means it is fully
testable offline.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .contracts import (
    ActionDecision,
    ActionRequest,
    AuthorizationRequest,
    Decision,
    Finding,
    Severity,
    TaskAuthorization,
)


@runtime_checkable
class AdvisoryReviewer(Protocol):
    """Produces non-binding commentary on audit activity."""

    def review_action(
        self,
        request: ActionRequest,
        decision: Decision,
        findings: list[Finding],
    ) -> list[str]:
        """Comment on one adjudication. Must not raise."""
        ...

    def review_authorization(
        self,
        request: AuthorizationRequest,
        authorization: TaskAuthorization,
    ) -> list[str]:
        """Comment on one grant. Must not raise."""
        ...


class NoOpReviewer:
    """The default: no model, no commentary, no failure modes.

    Used whenever no reviewer is configured, which is the correct default
    for tests, for offline runs, and for any deployment that has not
    deliberately opted into advisory review.
    """

    def review_action(
        self,
        request: ActionRequest,
        decision: Decision,
        findings: list[Finding],
    ) -> list[str]:
        return []

    def review_authorization(
        self,
        request: AuthorizationRequest,
        authorization: TaskAuthorization,
    ) -> list[str]:
        return []


class HeuristicReviewer:
    """Deterministic pattern flags, no model required.

    Gives the advisory channel real content in an offline demo: things a
    human reviewer would want surfaced, computed by code so they are
    reproducible. Still non-binding — these never affect the verdict.
    """

    #: How many denied calls in a row before it looks like an agent
    #: probing for a way around a limit rather than adjusting to one.
    DENY_STREAK_THRESHOLD = 3

    def __init__(self) -> None:
        self._deny_streak: dict[str, int] = {}
        self._recent: dict[str, list[str]] = {}

    def review_action(
        self,
        request: ActionRequest,
        decision: Decision,
        findings: list[Finding],
    ) -> list[str]:
        notes: list[str] = []
        key = f"{request.app_id}:{request.agent_id}"
        streak = self._deny_streak.get(key, 0)
        if decision is Decision.DENY:
            streak += 1
            self._deny_streak[key] = streak
            if streak >= self.DENY_STREAK_THRESHOLD:
                notes.append(
                    f"this agent has been denied {streak} times in a row; "
                    "repeated denials may indicate it is probing the limit "
                    "rather than adjusting to it"
                )
        else:
            self._deny_streak[key] = 0

        critical = [f for f in findings if f.severity is Severity.CRITICAL]
        if critical and decision is Decision.ALLOW:
            notes.append(
                "action allowed despite a critical-severity finding; worth a "
                "human look: " + "; ".join(f.message for f in critical[:2])
            )

        history = self._recent.setdefault(key, [])
        history.append(request.tool)
        del history[:-10]
        if len(set(history)) == 1 and len(history) >= 5:
            notes.append(
                f"last {len(history)} calls all target '{request.tool}'; a "
                "narrow retry loop can mean the agent is not using the "
                "reason it was given"
            )
        return notes

    def review_authorization(
        self,
        request: AuthorizationRequest,
        authorization: TaskAuthorization,
    ) -> list[str]:
        notes: list[str] = []
        if authorization.decision is Decision.ALLOW and not authorization.permissions:
            notes.append(
                "grant approved with no tools; the task may be mis-specified "
                "or the policy may not cover it"
            )
        requested = set(request.requested_tools)
        granted = set(authorization.tool_names())
        withheld = sorted(requested - granted)
        if withheld:
            notes.append(
                "requested but not granted: "
                + ", ".join(withheld)
                + " — the action agent should plan without these"
            )
        if not request.task_summary.strip():
            notes.append(
                "no task intent was stated, so the owner brief is thin; "
                "agents that declare intent get faster approvals"
            )
        return notes


class QwenAdvisoryReviewer:
    """Model-backed commentary, strictly non-binding.

    Wraps a callable so the model integration stays swappable and the class
    remains importable without `qwen-agent` installed. The callable takes a
    prompt string and returns text; failures produce no notes.

    The prompt is deliberately constrained: the model is asked to flag
    concerns for a human, is told it cannot change the outcome, and is
    shown the decision that has already been made. Asking it to decide
    would invite it to reason itself into overriding a deterministic gate.
    """

    MAX_NOTES = 5

    def __init__(
        self,
        complete: Any = None,
        *,
        model: str = "qwen-plus",
        max_input_chars: int = 6000,
    ) -> None:
        self._complete = complete
        self.model = model
        self.max_input_chars = max_input_chars

    # -- Model access -----------------------------------------------------

    def _callable(self) -> Any:
        if self._complete is not None:
            return self._complete
        # Lazily resolve a DashScope-backed completion function. Import and
        # construction are both optional; absence just means no commentary.
        try:
            from qwen_agent.llm import get_chat_model
        except Exception:
            return None
        try:
            llm = get_chat_model(
                {
                    "model": self.model,
                    "model_server": "dashscope",
                    # The API key is read from the environment by the
                    # library; it is never passed through this code and
                    # never logged.
                    "generate_cfg": {"max_input_tokens": 4000},
                }
            )
        except Exception:
            return None

        def complete(prompt: str) -> str:
            response = llm.chat(
                messages=[{"role": "user", "content": prompt}],
            )
            parts = []
            for message in response:
                content = message.get("content")
                if isinstance(content, str):
                    parts.append(content)
            return "\n".join(parts).strip()

        self._complete = complete
        return complete

    # -- Prompting --------------------------------------------------------

    def _prompt(self, subject: str, body: str) -> str:
        body = body[: self.max_input_chars]
        return (
            "You are a reviewer assisting a human owner of a financial "
            "application. A deterministic audit gate has ALREADY decided the "
            "outcome below; you cannot change it and must not try to.\n\n"
            "Your only job: in at most three short bullet points, flag "
            "anything a human should look at — unusual amounts, tone that "
            "suggests a customer dispute, a pattern that suggests an agent "
            "is working around a limit, or missing context. If nothing needs "
            "attention, reply with the single word NONE.\n\n"
            f"{subject}\n\n{body}"
        )

    def _notes(self, subject: str, body: str) -> list[str]:
        complete = self._callable()
        if complete is None:
            return []
        try:
            text = complete(self._prompt(subject, body))
        except Exception:
            # No model, no key, no network, malformed reply: the decision
            # stands and the audit trail simply has no advisory notes.
            return []
        if not isinstance(text, str):
            return []
        text = text.strip()
        if not text or text.upper().startswith("NONE"):
            return []
        notes = []
        for line in text.splitlines():
            cleaned = line.strip().lstrip("-*• ").strip()
            if cleaned and cleaned.upper() != "NONE":
                notes.append(cleaned[:500])
            if len(notes) >= self.MAX_NOTES:
                break
        return notes

    # -- AdvisoryReviewer -------------------------------------------------

    def review_action(
        self,
        request: ActionRequest,
        decision: Decision,
        findings: list[Finding],
    ) -> list[str]:
        body = (
            f"Decision: {decision.value}\n"
            f"Tool: {request.tool}\n"
            f"Task: {request.task_type}\n"
            f"Arguments: {request.arguments}\n"
            f"Findings: {[f.message for f in findings]}\n"
        )
        return self._notes("Subject: one agent action", body)

    def review_authorization(
        self,
        request: AuthorizationRequest,
        authorization: TaskAuthorization,
    ) -> list[str]:
        body = (
            f"Decision: {authorization.decision.value}\n"
            f"Task: {request.task_type}\n"
            f"Intent: {request.task_summary}\n"
            f"Tools requested: {request.requested_tools}\n"
            f"Tools granted: {authorization.tool_names()}\n"
            f"Findings: {[f.message for f in authorization.findings]}\n"
        )
        return self._notes("Subject: a task authorization grant", body)


class CombinedReviewer:
    """Runs several reviewers and concatenates their notes.

    Lets a deployment keep the deterministic heuristics (always available,
    reproducible) and add model commentary when a key is present.
    """

    def __init__(self, reviewers: list[AdvisoryReviewer]) -> None:
        self.reviewers = list(reviewers)

    def review_action(
        self,
        request: ActionRequest,
        decision: Decision,
        findings: list[Finding],
    ) -> list[str]:
        notes: list[str] = []
        for reviewer in self.reviewers:
            try:
                notes.extend(reviewer.review_action(request, decision, findings))
            except Exception:  # advisory must never break a decision
                continue
        return notes

    def review_authorization(
        self,
        request: AuthorizationRequest,
        authorization: TaskAuthorization,
    ) -> list[str]:
        notes: list[str] = []
        for reviewer in self.reviewers:
            try:
                notes.extend(
                    reviewer.review_authorization(request, authorization)
                )
            except Exception:
                continue
        return notes


__all__ = [
    "AdvisoryReviewer",
    "CombinedReviewer",
    "HeuristicReviewer",
    "NoOpReviewer",
    "QwenAdvisoryReviewer",
]

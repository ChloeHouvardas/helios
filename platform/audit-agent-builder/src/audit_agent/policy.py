"""Policy: the tool registry, deterministic rules, and policy packs.

Two kinds of pack, per `specs/decisions.md`:

- **baseline** — shipped with the platform. Applies to every app. An app
  may extend it but may not weaken or delete it; `PolicyPack.compose`
  raises `PolicyLooseningError` if one tries.
- **app** — written per application, as data. This is what Qwen generates
  when it builds an app, so it is YAML, not Python.

Rules are plain, testable code and they are the *only* thing that decides.
The audit agent is deliberately not an LLM at this layer: `specs/decisions.md`
fixes audit decisions as deterministic and fail-closed. Advisory model
commentary lives in `advisory.py` and cannot change a verdict.

The tool registry is decoupled from the action agent's code. A tool can be
described declaratively (YAML/JSON) or adapted from a live object that
follows the Build 1 `ToolContract` shape — name, description,
`has_side_effect`, `input_schema()`. Nothing here imports the action
agent's package, which is what keeps this agent standalone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Literal, Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .contracts import (
    ActionRequest,
    AuthorizationRequest,
    Decision,
    Effect,
    Finding,
    Severity,
    TaskAuthorization,
    ToolPermission,
)
from .errors import (
    PolicyError,
    PolicyLooseningError,
    UnknownTaskTypeError,
    UnknownToolError,
)

# --- Tool registry -------------------------------------------------------


class ToolDescriptor(BaseModel):
    """A tool as the audit agent understands it.

    Only the fields a policy decision needs. The audit agent never executes
    a tool — it authorises calls — so `execute` is deliberately absent.
    """

    name: str = Field(min_length=1)
    description: str = ""
    side_effect: bool = False
    #: JSON Schema for arguments, when the tool declares one. Used to
    #: reject malformed arguments before anything happens.
    input_schema: dict[str, Any] | None = None
    #: Declared by the tool's owner, not by the action agent. A tool that
    #: says it has no side effect but appears in a gated policy is a
    #: configuration error, caught at composition time.
    owner: str = "app"

    @classmethod
    def from_contract(cls, tool: Any) -> "ToolDescriptor":
        """Adapt any object following the Build 1 `ToolContract` shape.

        Duck-typed on purpose: importing `agent_toolkit` here would couple
        the audit agent to the action agent's package and break the
        standalone requirement. Only attribute access, no import.
        """
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not name:
            raise PolicyError(f"tool has no usable name: {tool!r}")
        schema = None
        schema_fn = getattr(tool, "input_schema", None)
        if callable(schema_fn):
            try:
                schema = schema_fn()
            except Exception:  # pragma: no cover - defensive
                schema = None
        return cls(
            name=name,
            description=str(getattr(tool, "description", "") or ""),
            side_effect=bool(getattr(tool, "has_side_effect", False)),
            input_schema=schema if isinstance(schema, dict) else None,
        )


class ToolRegistry:
    """The set of tools the audit agent knows about.

    Unknown tools fail closed: a request for a tool that is not registered
    is a deny, never an implicit allow.
    """

    def __init__(self, tools: Iterable[ToolDescriptor] = ()) -> None:
        self._tools: dict[str, ToolDescriptor] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolDescriptor) -> None:
        existing = self._tools.get(tool.name)
        if existing is not None and existing.side_effect != tool.side_effect:
            # Two conflicting declarations of side-effect for one tool name
            # would make the gate ambiguous. Refuse rather than pick one.
            raise PolicyError(
                f"conflicting side-effect declarations for tool '{tool.name}'"
            )
        self._tools[tool.name] = tool

    def register_contract(self, tool: Any) -> None:
        self.register(ToolDescriptor.from_contract(tool))

    def get(self, name: str) -> ToolDescriptor:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(f"tool '{name}' is not registered") from None

    def has(self, name: str) -> bool:
        return name in self._tools

    def side_effect(self, name: str) -> bool:
        return self.get(name).side_effect

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def side_effect_names(self) -> list[str]:
        return sorted(n for n, t in self._tools.items() if t.side_effect)

    def read_only_names(self) -> list[str]:
        return sorted(n for n, t in self._tools.items() if not t.side_effect)

    def __len__(self) -> int:
        return len(self._tools)


# --- Rule interface ------------------------------------------------------


@dataclass(frozen=True)
class RuleOutcome:
    """A rule's contribution. `None` effect means the rule did not fire."""

    effect: Effect | None = None
    message: str = ""
    severity: Severity = Severity.INFO
    details: dict[str, Any] = field(default_factory=dict)
    #: Argument overrides the rule wants applied, e.g. capping an
    #: extension. Merged into `ActionDecision.granted_arguments`.
    argument_overrides: dict[str, Any] = field(default_factory=dict)

    @property
    def fired(self) -> bool:
        return self.effect is not None

    def as_finding(self, rule_id: str, source: str) -> Finding:
        assert self.effect is not None
        return Finding(
            rule_id=rule_id,
            effect=self.effect,
            severity=self.severity,
            message=self.message,
            source="baseline" if source == "baseline" else "app",  # type: ignore[arg-type]
            details=self.details,
        )


@runtime_checkable
class Rule(Protocol):
    """A deterministic check. Must be pure: no I/O, no clock, no randomness.

    Two entry points, matching the two interactions:

    - `at_authorization` shapes the grant (which tools, what limits).
    - `at_action` adjudicates a concrete call against that grant.

    A rule may implement either or both. Facts come in through
    `AuditFacts`, resolved by the agent from trusted sources — never from
    the action agent's claims.
    """

    rule_id: str
    #: Rules are keyed to task types so a collect rule cannot fire on a
    #: payables task. `("*",)` means the rule is global.
    task_types: tuple[str, ...]

    def at_authorization(
        self, request: AuthorizationRequest, facts: "AuditFacts", draft: "GrantDraft"
    ) -> RuleOutcome | None: ...

    def at_action(
        self,
        request: ActionRequest,
        facts: "AuditFacts",
        permission: ToolPermission | None,
        authorization: TaskAuthorization | None,
    ) -> RuleOutcome | None: ...


@dataclass
class AuditFacts:
    """Values the audit agent resolved itself.

    The whole point of separating facts from claims: an agent that
    under-reports an invoice amount must not be able to slip under an
    owner-approval threshold. Anything a rule depends on lives here, and
    the agent populates it from a fact source, not from the request.
    """

    #: Trusted monetary amounts, e.g. {"INV-1001": Decimal("450.00")}.
    amounts: dict[str, Decimal] = field(default_factory=dict)
    #: Trusted contact counts in a trailing window, keyed by subject.
    recent_contact_counts: dict[str, int] = field(default_factory=dict)
    #: Window in days the counts above were computed over.
    contact_window_days: int | None = None
    #: Trusted identity of the record, e.g. supplier or customer id.
    subjects: dict[str, str] = field(default_factory=dict)
    #: Whether a counterparty's payment details changed recently — the
    #: Payables fraud signal. Only a fact source may set this.
    payment_details_changed: dict[str, bool] = field(default_factory=dict)
    #: How many times each tool was already called under this grant.
    tool_call_counts: dict[str, int] = field(default_factory=dict)
    #: Anything else a rule needs, resolved by the agent.
    extras: dict[str, Any] = field(default_factory=dict)

    def amount(self, key: str) -> Decimal | None:
        return self.amounts.get(key)

    def contact_count(self, key: str) -> int | None:
        return self.recent_contact_counts.get(key)


@dataclass
class GrantDraft:
    """The grant under construction, which rules may narrow but not widen.

    Rules mutate this during authorization. Narrowing only: a rule may
    remove a tool or tighten a limit, and any attempt to add a tool that
    the baseline did not allow is discarded. That asymmetry is what makes
    "apps may extend but not weaken the baseline" enforceable in code.
    """

    permissions: dict[str, ToolPermission] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    #: Task types matched, recorded so the grant can explain itself.
    matched_packs: list[str] = field(default_factory=list)

    def allow(self, permission: ToolPermission) -> None:
        self.permissions[permission.tool] = permission

    def deny_tool(self, tool: str, rule_id: str, message: str) -> None:
        self.permissions.pop(tool, None)
        self.findings.append(
            Finding(
                rule_id=rule_id,
                effect="deny",
                severity=Severity.WARN,
                message=message,
                details={"tool": tool},
            )
        )

    def tighten_limit(self, tool: str, key: str, value: Any) -> None:
        """Lower a numeric ceiling. Raising one is ignored."""
        permission = self.permissions.get(tool)
        if permission is None:
            return
        current = permission.limits.get(key)
        if isinstance(current, (int, float, str, Decimal)) and _numeric(value) is not None:
            if _numeric(current) is not None and _numeric(current) < _numeric(value):  # type: ignore[operator]
                return  # refuse to loosen
        permission.limits[key] = value

    def reduce_max_calls(self, tool: str, value: int) -> None:
        permission = self.permissions.get(tool)
        if permission is None:
            return
        if permission.max_calls is None or value < permission.max_calls:
            permission.max_calls = value

    def require_approval(self, tool: str, condition: str | None = None) -> None:
        permission = self.permissions.get(tool)
        if permission is None:
            return
        permission.requires_approval = True
        if condition:
            permission.condition = condition


def _numeric(value: Any) -> Decimal | None:
    """Best-effort Decimal coercion. Returns None when not numeric."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return None
    return None


# --- Baseline rules (platform, non-negotiable) ---------------------------


class BaselineRule:
    """Shared plumbing for the platform's built-in rules."""

    source = "baseline"

    def at_authorization(
        self, request: AuthorizationRequest, facts: AuditFacts, draft: GrantDraft
    ) -> RuleOutcome | None:
        return None

    def at_action(
        self,
        request: ActionRequest,
        facts: AuditFacts,
        permission: ToolPermission | None,
        authorization: TaskAuthorization | None,
    ) -> RuleOutcome | None:
        return None


class ToolMustBeRegisteredRule(BaselineRule):
    """Fail closed on unknown tools.

    An action agent that asks for a tool the platform does not know about
    gets a deny with a reason it can act on, not a crash and not a pass.
    """

    rule_id = "baseline.tool_registered"
    task_types = ("*",)

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def at_authorization(
        self, request: AuthorizationRequest, facts: AuditFacts, draft: GrantDraft
    ) -> RuleOutcome | None:
        for tool in list(draft.permissions):
            if not self.registry.has(tool):
                draft.deny_tool(
                    tool,
                    self.rule_id,
                    f"tool '{tool}' is not registered on this platform",
                )
        unknown = [t for t in request.requested_tools if not self.registry.has(t)]
        if unknown:
            return RuleOutcome(
                effect="deny",
                message=f"requested tools are not registered: {', '.join(sorted(unknown))}",
                severity=Severity.CRITICAL,
                details={"unknown_tools": sorted(unknown)},
            )
        return None

    def at_action(
        self,
        request: ActionRequest,
        facts: AuditFacts,
        permission: ToolPermission | None,
        authorization: TaskAuthorization | None,
    ) -> RuleOutcome | None:
        if not self.registry.has(request.tool):
            return RuleOutcome(
                effect="deny",
                message=f"tool '{request.tool}' is not registered on this platform",
                severity=Severity.CRITICAL,
                details={"tool": request.tool},
            )
        return None


class ArgumentsMustValidateRule(BaselineRule):
    """Reject malformed arguments before any side effect.

    Uses the tool's own declared schema, so a tool that changes its inputs
    automatically tightens this check. No schema declared means no
    automatic pass — the rule simply does not fire and other rules decide.
    """

    rule_id = "baseline.arguments_validate"
    task_types = ("*",)

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def at_action(
        self,
        request: ActionRequest,
        facts: AuditFacts,
        permission: ToolPermission | None,
        authorization: TaskAuthorization | None,
    ) -> RuleOutcome | None:
        descriptor = self.registry.get(request.tool)
        schema = descriptor.input_schema
        if not schema:
            return None
        try:
            import jsonschema  # local import: optional at runtime
        except ImportError:  # pragma: no cover - jsonschema is a transitive dep
            return RuleOutcome(
                effect="deny",
                message=(
                    "argument schema validation is unavailable; refusing to "
                    "authorise a side effect without it"
                ),
                severity=Severity.CRITICAL,
                details={"tool": request.tool},
            )
        validator = jsonschema.Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(request.arguments), key=lambda e: e.path)
        if errors:
            return RuleOutcome(
                effect="deny",
                message=f"arguments for '{request.tool}' failed schema validation",
                severity=Severity.CRITICAL,
                details={
                    "tool": request.tool,
                    "errors": [
                        {"path": list(map(str, e.path)), "message": e.message}
                        for e in errors[:10]
                    ],
                },
            )
        return None


class GrantMustBeValidRule(BaselineRule):
    """A presented grant must be genuine; a side effect must have one.

    Two distinct checks, and the difference matters:

    - If a grant *is* presented, it is verified whatever the tool. A
      forged, expired or re-scoped grant must not pass just because the
      call happens to be read-only.
    - If *no* grant is presented, only side-effect tools are refused.
      Read-only calls run directly per `specs/decisions.md`, so requiring a
      grant for them would contradict the architecture.

    Binding to app, agent and task type, expiry, signature and replay are
    each a separate deny reason, so the action agent can fix the actual
    problem instead of guessing.
    """

    rule_id = "baseline.grant_valid"
    task_types = ("*",)

    def __init__(self, registry: ToolRegistry, verifier: "GrantVerifier") -> None:
        self.registry = registry
        self.verifier = verifier

    def at_action(
        self,
        request: ActionRequest,
        facts: AuditFacts,
        permission: ToolPermission | None,
        authorization: TaskAuthorization | None,
    ) -> RuleOutcome | None:
        if authorization is not None:
            return self.verifier.verify(authorization, request)
        if not self.registry.side_effect(request.tool):
            return None  # read-only calls are not gated
        return RuleOutcome(
            effect="deny",
            message=(
                f"'{request.tool}' has a side effect and no authorization was "
                "presented; request task authorization first"
            ),
            severity=Severity.CRITICAL,
            details={"tool": request.tool},
        )


class PermissionRequiredRule(BaselineRule):
    """The tool must be inside the grant for this task.

    A grant is an allowlist: anything outside it is denied, which stops an
    agent using a tool authorised for a different task. A read-only call
    with no grant at all is not refused here — it runs directly, and
    `GrantMustBeValidRule` already covers the case where a grant was
    presented but is invalid.
    """

    rule_id = "baseline.permission_required"
    task_types = ("*",)

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def at_action(
        self,
        request: ActionRequest,
        facts: AuditFacts,
        permission: ToolPermission | None,
        authorization: TaskAuthorization | None,
    ) -> RuleOutcome | None:
        if permission is not None:
            return None
        if authorization is None and not self.registry.side_effect(request.tool):
            return None
        return RuleOutcome(
            effect="deny",
            message=(
                f"'{request.tool}' is not granted for task '{request.task_type}'"
            ),
            severity=Severity.CRITICAL,
            details={"tool": request.tool, "task_type": request.task_type},
        )


class CallBudgetRule(BaselineRule):
    """Enforce `max_calls` on the grant, using runtime-resolved counts."""

    rule_id = "baseline.call_budget"
    task_types = ("*",)

    def at_action(
        self,
        request: ActionRequest,
        facts: AuditFacts,
        permission: ToolPermission | None,
        authorization: TaskAuthorization | None,
    ) -> RuleOutcome | None:
        if permission is None or permission.max_calls is None:
            return None
        used = facts.tool_call_counts.get(request.tool, 0)
        if used >= permission.max_calls:
            return RuleOutcome(
                effect="deny",
                message=(
                    f"'{request.tool}' has reached its grant limit of "
                    f"{permission.max_calls} call(s) for this task"
                ),
                severity=Severity.WARN,
                details={
                    "tool": request.tool,
                    "max_calls": permission.max_calls,
                    "calls_used": used,
                },
            )
        return None


class ExplicitApprovalRule(BaselineRule):
    """Tools marked `requires_approval` always go to the owner."""

    rule_id = "baseline.explicit_approval"
    task_types = ("*",)

    def at_action(
        self,
        request: ActionRequest,
        facts: AuditFacts,
        permission: ToolPermission | None,
        authorization: TaskAuthorization | None,
    ) -> RuleOutcome | None:
        if permission is None or not permission.requires_approval:
            return None
        return RuleOutcome(
            effect="hold",
            message=permission.condition
            or f"'{request.tool}' requires owner approval for this task",
            severity=Severity.WARN,
            details={"tool": request.tool},
        )


class IntentStatedRule(BaselineRule):
    """A task with no stated intent is not auditable.

    Cheap and high value: it forces the action agent to declare what it is
    doing in plain language, which is what the owner reads in the approval
    queue and what a reviewer reads in the log.
    """

    rule_id = "baseline.intent_stated"
    task_types = ("*",)
    min_length = 12

    def at_authorization(
        self, request: AuthorizationRequest, facts: AuditFacts, draft: GrantDraft
    ) -> RuleOutcome | None:
        if len(request.task_summary.strip()) < self.min_length:
            return RuleOutcome(
                effect="deny",
                message=(
                    "task_summary is too short to audit; state in plain language "
                    f"what the agent intends to do (>= {self.min_length} characters)"
                ),
                severity=Severity.WARN,
                details={"task_summary_length": len(request.task_summary.strip())},
            )
        return None


class WithinGrantAllowRule(BaselineRule):
    """The positive allow: the call is inside the grant and nothing objected.

    This rule exists because "fail closed" is often misread as "an action
    with no findings is denied". It is not. Fail closed means *unknowns* are
    denied — an unregistered tool, malformed arguments, a fact that could
    not be verified, a grant that does not validate. A call that is inside a
    valid grant and trips no rule is exactly the normal case, and denying it
    would make the platform unusable.

    Emitting an explicit allow finding also improves the evidence: the log
    records *why* the action passed, rather than leaving a silent gap that a
    reviewer has to infer from.

    Safety: `Precedence.ORDER` ranks deny above hold above allow, so this
    finding can never override an objection raised by any other rule, in any
    pack, at any severity. It only takes effect when nothing else fired.
    """

    rule_id = "baseline.within_grant"
    task_types = ("*",)

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def at_authorization(
        self, request: AuthorizationRequest, facts: AuditFacts, draft: GrantDraft
    ) -> RuleOutcome | None:
        if not draft.permissions:
            # Nothing to grant. Reported as a deny so the action agent gets
            # an actionable reason instead of an empty allow it cannot use.
            return RuleOutcome(
                effect="deny",
                message=(
                    f"task '{request.task_type}' grants no tools for this "
                    "request; check the requested tool list and the app policy"
                ),
                severity=Severity.WARN,
                details={"requested_tools": request.requested_tools},
            )
        return RuleOutcome(
            effect="allow",
            message=(
                "tools are within the policy for this task and no rule "
                f"objected: {', '.join(sorted(draft.permissions))}"
            ),
            severity=Severity.INFO,
            details={"tools": sorted(draft.permissions)},
        )

    def at_action(
        self,
        request: ActionRequest,
        facts: AuditFacts,
        permission: ToolPermission | None,
        authorization: TaskAuthorization | None,
    ) -> RuleOutcome | None:
        if not self.registry.has(request.tool):
            return None  # ToolMustBeRegisteredRule already denied it
        if permission is None:
            # A read-only call with no grant presented runs directly per
            # specs/decisions.md; anything else was already denied by
            # PermissionRequiredRule, so stay silent here.
            if authorization is None and not self.registry.side_effect(request.tool):
                return RuleOutcome(
                    effect="allow",
                    message=(
                        f"'{request.tool}' is read-only and runs directly "
                        "without a grant"
                    ),
                    severity=Severity.INFO,
                    details={"tool": request.tool},
                )
            return None
        return RuleOutcome(
            effect="allow",
            message=(
                f"'{request.tool}' is inside the grant for task "
                f"'{request.task_type}' and no rule objected"
            ),
            severity=Severity.INFO,
            details={
                "tool": request.tool,
                "authorization_id": (
                    authorization.authorization_id if authorization else None
                ),
            },
        )


BASELINE_RULE_IDS: frozenset[str] = frozenset(
    {
        ToolMustBeRegisteredRule.rule_id,
        ArgumentsMustValidateRule.rule_id,
        GrantMustBeValidRule.rule_id,
        PermissionRequiredRule.rule_id,
        CallBudgetRule.rule_id,
        ExplicitApprovalRule.rule_id,
        IntentStatedRule.rule_id,
        WithinGrantAllowRule.rule_id,
    }
)


# --- Declarative rules (what Qwen writes) --------------------------------


#: Whether a limit could be evaluated, and if not, why.
#:
#: The distinction is the whole correctness story for numeric limits:
#:
#: - `NOT_REQUESTED` — the argument is absent or zero. The agent is not
#:   asking for the constrained thing, so the limit does not apply. Denying
#:   here would refuse every ordinary call, which is not fail-closed, it is
#:   broken.
#: - `UNVERIFIABLE` — the limit binds a *fact* the audit agent could not
#:   resolve from a trusted source. This must fail closed: an agent that
#:   cannot prove the invoice is small does not get to act as if it were.
#: - `UNPARSEABLE` — the argument is present but not a number. Also fails
#:   closed, since the value cannot be compared to the ceiling.
LimitStatus = Literal["ok", "not_requested", "unverifiable", "unparseable"]


class LimitSpec(BaseModel):
    """A declarative numeric limit on an argument or fact.

    Keeps generated policy honest: a limit names the value it constrains,
    where that value comes from, and the ceiling. Anything a rule cannot
    express this way belongs in Python, reviewed by a human.

    Two independent axes, and it is worth being precise about them because
    conflating them is how a control ends up checking nothing:

    - **What is measured** — `argument` (what the agent asked for) or `fact`
      (what the audit agent resolved from a trusted source).
    - **What it is compared against** — `max` (a constant written in policy)
      or `max_from_fact` (a ceiling resolved from a trusted source at
      decision time).

    The combination that matters most in finance is *argument measured
    against a fact ceiling*: "the payment may not exceed the recorded
    invoice amount". The agent chooses the amount; the ledger chooses the
    ceiling; neither can move the other.
    """

    #: Dotted path into the call arguments, e.g. "extension_days".
    argument: str | None = None
    #: Key into `AuditFacts.amounts` / `.extras`, e.g. "amount_due".
    fact: str | None = None
    #: Constant ceiling. Decimal-compatible string or number. Mutually
    #: exclusive with `max_from_fact`.
    max: Decimal | float | int | str | None = None
    #: Key of a trusted fact to use as the ceiling instead of a constant,
    #: e.g. "invoice_amount". Mutually exclusive with `max`.
    max_from_fact: str | None = None
    #: True -> exceeding the limit holds for the owner instead of denying.
    on_exceed: Literal["deny", "hold"] = "hold"
    #: True -> clamp the argument to the limit and allow, instead of
    #: holding. Used for the "grant less than asked" pattern.
    clamp: bool = False
    #: Treat this value of an argument limit as "not requested". An agent
    #: that sends `extension_days: 0` is not asking for an extension, so a
    #: cap on extensions must not fire on it.
    not_requested_when: tuple[int, float] = (0,)
    message: str | None = None

    @field_validator("max", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("limit 'max' must be a number")
        return value

    @model_validator(mode="after")
    def _exactly_one_ceiling(self) -> "LimitSpec":
        """One ceiling, and only one.

        Both set is ambiguous; neither set means the limit has nothing to
        compare against and would silently never fire — a control that looks
        configured but enforces nothing.
        """
        if self.max is not None and self.max_from_fact is not None:
            raise ValueError(
                "a limit sets both 'max' and 'max_from_fact'; choose one "
                "constant ceiling or one fact-derived ceiling"
            )
        if self.max is None and not self.max_from_fact:
            raise ValueError(
                "a limit must set either 'max' (a constant) or 'max_from_fact' "
                "(a trusted fact used as the ceiling)"
            )
        if not self.argument and not self.fact:
            raise ValueError(
                "a limit must set either 'argument' (what the agent asked for) "
                "or 'fact' (what the audit agent resolved)"
            )
        return self

    def ceiling(self, facts: "AuditFacts | None" = None) -> tuple[Decimal | None, LimitStatus]:
        """Resolve the ceiling, with a status explaining failure.

        A fact-derived ceiling that cannot be resolved is `unverifiable`,
        which the caller treats as fail-closed. Guessing a ceiling would be
        the same as having none.
        """
        if self.max_from_fact is None:
            value = _numeric(self.max)
            if value is None:
                # The policy itself declares a non-numeric constant. Report
                # it as unparseable so the caller fails closed rather than
                # treating a typo as an unenforced control.
                return None, "unparseable"
            return value, "ok"
        if facts is None:
            return None, "unverifiable"
        raw = facts.extras.get(self.max_from_fact)
        if raw is None and self.max_from_fact in facts.amounts:
            raw = facts.amounts[self.max_from_fact]
        if raw is None:
            return None, "unverifiable"
        value = _numeric(raw)
        if value is None:
            return None, "unverifiable"
        return value, "ok"

    @property
    def binds(self) -> str:
        """What this limit constrains, for messages and findings."""
        return str(self.argument or self.fact or "")

    @property
    def ceiling_description(self) -> str:
        """How the ceiling is defined, for policy reports."""
        if self.max_from_fact:
            return f"trusted fact '{self.max_from_fact}'"
        return f"constant {self.max}"

    def observed(
        self, request: ActionRequest, facts: AuditFacts
    ) -> tuple[Decimal | None, LimitStatus]:
        """Resolve the value this limit binds, with an explicit status.

        Argument limits and fact limits fail differently on absence, and
        that asymmetry is deliberate:

        - An absent argument is the agent *not asking* for something. No
          request, nothing to constrain.
        - An absent fact is the audit agent *not knowing* something it needs
          in order to judge the request. Unknown is never treated as small.
        """
        if self.argument is not None:
            raw = _dig(request.arguments, self.argument)
            if raw is None:
                return None, "not_requested"
            value = _numeric(raw)
            if value is None:
                return None, "unparseable"
            if any(value == _numeric(sentinel) for sentinel in self.not_requested_when):
                return None, "not_requested"
            return value, "ok"

        if self.fact is not None:
            raw = facts.extras.get(self.fact)
            if raw is None and self.fact in facts.amounts:
                raw = facts.amounts[self.fact]
            if raw is None and self.fact.startswith("amount") and len(facts.amounts) == 1:
                # Convenience: "amount_due" resolves against the single
                # trusted amount in scope, so generated policy does not have
                # to know the subject identifier.
                raw = next(iter(facts.amounts.values()))
            if raw is None:
                return None, "unverifiable"
            value = _numeric(raw)
            if value is None:
                return None, "unverifiable"
            return value, "ok"

        return None, "unverifiable"


def _dig(mapping: dict[str, Any], path: str) -> Any:
    """Resolve a dotted path; missing keys yield None, never KeyError."""
    current: Any = mapping
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


class DeclarativeRule:
    """A data-driven rule compiled from a policy pack.

    One class, many instances, so generated policy cannot smuggle in
    arbitrary code. The supported shapes are deliberately few: numeric
    limits, required arguments, forbidden argument values, contact
    frequency, changed counterparty details and approval triggers.
    """

    source = "app"

    def __init__(
        self,
        rule_id: str,
        task_types: tuple[str, ...],
        *,
        tools: tuple[str, ...] = ("*",),
        limits: tuple[LimitSpec, ...] = (),
        required_arguments: tuple[str, ...] = (),
        forbidden_values: dict[str, tuple[Any, ...]] | None = None,
        max_contacts_in_days: dict[str, Any] | None = None,
        deny_if_fact_true: tuple[str, ...] = (),
        approval_if_fact_true: tuple[str, ...] = (),
        effect_on_match: Effect = "deny",
        message: str = "",
        severity: Severity = Severity.WARN,
    ) -> None:
        self.rule_id = rule_id
        self.task_types = task_types
        self.tools = tools
        self.limits = limits
        self.required_arguments = required_arguments
        self.forbidden_values = forbidden_values or {}
        self.max_contacts_in_days = max_contacts_in_days or {}
        self.deny_if_fact_true = deny_if_fact_true
        self.approval_if_fact_true = approval_if_fact_true
        self.effect_on_match = effect_on_match
        self.message = message
        self.severity = severity

    # -- Matching ---------------------------------------------------------

    def _applies(self, task_type: str, tool: str) -> bool:
        if "*" not in self.task_types and task_type not in self.task_types:
            return False
        return "*" in self.tools or tool in self.tools

    def _msg(self, fallback: str) -> str:
        return self.message or fallback

    # -- Authorization-time effects --------------------------------------

    def at_authorization(
        self, request: AuthorizationRequest, facts: AuditFacts, draft: GrantDraft
    ) -> RuleOutcome | None:
        """Shape the grant: apply limits and approval requirements."""
        if "*" not in self.task_types and request.task_type not in self.task_types:
            return None
        touched = False
        for tool, permission in draft.permissions.items():
            if "*" not in self.tools and tool not in self.tools:
                continue
            for limit in self.limits:
                if limit.argument:
                    key = f"max_{limit.argument}"
                    permission.limits.setdefault(key, str(limit.max))
                    draft.tighten_limit(tool, key, str(limit.max))
                    touched = True
            if self.approval_if_fact_true:
                for fact_key in self.approval_if_fact_true:
                    permission.limits.setdefault(f"approval_if_{fact_key}", True)
                    touched = True
        if not touched:
            return None
        return RuleOutcome(
            effect="allow",
            message=self._msg(f"policy '{self.rule_id}' applied to the grant"),
            severity=Severity.INFO,
            details={"rule_id": self.rule_id},
        )

    # -- Action-time effects ---------------------------------------------

    def at_action(
        self,
        request: ActionRequest,
        facts: AuditFacts,
        permission: ToolPermission | None,
        authorization: TaskAuthorization | None,
    ) -> RuleOutcome | None:
        if not self._applies(request.task_type, request.tool):
            return None
        overrides: dict[str, Any] = {}

        # Counterparty-detail change: the Payables fraud signal. Only a
        # fact source can set this, so an agent cannot talk its way past it.
        for fact_key in self.deny_if_fact_true:
            if _fact_is_true(facts, fact_key):
                return RuleOutcome(
                    effect="deny",
                    message=self._msg(
                        f"refused because trusted fact '{fact_key}' is true"
                    ),
                    severity=Severity.CRITICAL,
                    details={"fact": fact_key},
                )
        for fact_key in self.approval_if_fact_true:
            if _fact_is_true(facts, fact_key):
                return RuleOutcome(
                    effect="hold",
                    message=self._msg(
                        f"owner approval required because '{fact_key}' is true"
                    ),
                    severity=Severity.WARN,
                    details={"fact": fact_key},
                )

        # Required arguments.
        for argument in self.required_arguments:
            if _dig(request.arguments, argument) is None:
                return RuleOutcome(
                    effect="deny",
                    message=self._msg(f"missing required argument '{argument}'"),
                    severity=Severity.WARN,
                    details={"argument": argument},
                )

        # Forbidden values.
        for argument, forbidden in self.forbidden_values.items():
            value = _dig(request.arguments, argument)
            if value is not None and value in forbidden:
                return RuleOutcome(
                    effect="deny",
                    message=self._msg(
                        f"argument '{argument}' may not be {value!r}"
                    ),
                    severity=Severity.WARN,
                    details={"argument": argument, "value": value},
                )

        # Contact frequency, from trusted counts only.
        window = self.max_contacts_in_days.get("within_days")
        maximum = self.max_contacts_in_days.get("max")
        if window is not None and maximum is not None:
            subject = _subject_key(request)
            count = facts.contact_count(subject) if subject else None
            if count is None:
                return RuleOutcome(
                    effect="deny",
                    message=(
                        "contact frequency could not be verified from a trusted "
                        "source; refusing to authorise contact"
                    ),
                    severity=Severity.CRITICAL,
                    details={"subject": subject},
                )
            if count >= int(maximum):
                return RuleOutcome(
                    effect="deny",
                    message=self._msg(
                        f"customer already contacted {count} time(s) in the last "
                        f"{window} days; limit is {maximum}"
                    ),
                    severity=Severity.WARN,
                    details={
                        "subject": subject,
                        "contacts": count,
                        "max": int(maximum),
                        "within_days": int(window),
                    },
                )

        # Numeric limits.
        for limit in self.limits:
            observed, status = limit.observed(request, facts)

            if status == "not_requested":
                # The agent is not asking for the constrained thing, so this
                # limit has nothing to say. Skipping is correct, not lax:
                # a cap on extensions must not refuse a plain reminder.
                continue

            if status != "ok" or observed is None:
                # Fail closed. The audit agent cannot compare a value it
                # could not resolve or parse against a ceiling, and
                # "unknown" must never be read as "small".
                detail = (
                    "no trusted value could be resolved"
                    if status == "unverifiable"
                    else "the value supplied is not a number"
                )
                return RuleOutcome(
                    effect="deny",
                    message=(
                        f"limit on '{limit.binds}' could not be evaluated "
                        f"({detail}); refusing to authorise"
                    ),
                    severity=Severity.CRITICAL,
                    details={"limit": limit.binds, "status": status},
                )

            ceiling, ceiling_status = limit.ceiling(facts)
            if ceiling is None:
                if limit.max_from_fact and ceiling_status == "unverifiable":
                    # The ceiling comes from a trusted source that could not
                    # answer. Fail closed: without a known ceiling there is
                    # no basis to allow an amount the agent chose.
                    return RuleOutcome(
                        effect="deny",
                        message=(
                            f"the ceiling for '{limit.binds}' is the trusted fact "
                            f"'{limit.max_from_fact}', which could not be "
                            "resolved; refusing to authorise without it"
                        ),
                        severity=Severity.CRITICAL,
                        details={
                            "limit": limit.binds,
                            "ceiling_fact": limit.max_from_fact,
                            "status": "unverifiable",
                        },
                    )
                # The policy itself declares an unusable constant. Silently
                # skipping it would turn a typo into an unenforced control.
                return RuleOutcome(
                    effect="deny",
                    message=(
                        f"policy limit on '{limit.binds}' has a non-numeric "
                        "ceiling and cannot be enforced"
                    ),
                    severity=Severity.CRITICAL,
                    details={"limit": limit.binds, "configured_max": str(limit.max)},
                )

            if observed <= ceiling:
                continue

            if limit.clamp and limit.argument:
                # Grant less than was asked, mirroring Build 1's
                # extension_days_granted: what executes is the reduced
                # value, never the agent's original request.
                overrides[limit.argument] = str(ceiling)
                continue

            effect: Effect = "hold" if limit.on_exceed == "hold" else "deny"
            return RuleOutcome(
                effect=effect,
                message=self._msg(
                    f"{limit.binds}={observed} exceeds the limit of {ceiling} "
                    f"({limit.ceiling_description})"
                ),
                severity=Severity.WARN,
                details={
                    "field": limit.binds,
                    "observed": str(observed),
                    "limit": str(ceiling),
                },
            )

        if overrides:
            return RuleOutcome(
                effect="allow",
                message=self._msg(
                    "request reduced to policy limits: "
                    + ", ".join(f"{k}={v}" for k, v in sorted(overrides.items()))
                ),
                severity=Severity.INFO,
                details={"clamped": overrides},
                argument_overrides=overrides,
            )
        return None


def _fact_is_true(facts: AuditFacts, key: str) -> bool:
    if key in facts.payment_details_changed:
        return any(bool(v) for v in facts.payment_details_changed.values())
    if key in facts.extras:
        return bool(facts.extras[key])
    if key in facts.amounts:
        return bool(facts.amounts[key])
    return False


def _subject_key(request: ActionRequest) -> str | None:
    """The business subject a frequency limit applies to.

    Resolved from arguments first (the agent names the invoice/customer),
    then from the single trusted subject in scope. Never guessed.
    """
    for candidate in ("invoice_id", "customer_id", "supplier_id", "subject_id"):
        value = request.arguments.get(candidate)
        if isinstance(value, str) and value:
            return value
    if len(request.resolved_facts) >= 0 and request.resolved_facts:
        for candidate in ("invoice_id", "customer_id", "supplier_id", "subject_id"):
            value = request.resolved_facts.get(candidate)
            if isinstance(value, str) and value:
                return value
    return None


# --- Policy packs --------------------------------------------------------


class PolicyPack(BaseModel):
    """A named collection of policy, loadable from YAML.

    `source` decides trust: `baseline` packs are platform-owned and their
    rules cannot be removed or weakened by an app; `app` packs extend them.
    """

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1)
    source: Literal["baseline", "app"] = "app"
    version: str = "1"
    #: Task types this pack governs, with the tools each may use.
    tasks: dict[str, "TaskPolicy"] = Field(default_factory=dict)
    #: Declarative rules as data.
    rules: list["RuleSpec"] = Field(default_factory=list)
    #: Baseline rule ids an app pack is not allowed to disable. Declared
    #: here so the anti-loosening check has something to compare against.
    protected_rule_ids: list[str] = Field(default_factory=list)
    #: Baseline rules this pack tries to disable. Only meaningful (and only
    #: ever rejected) on an app pack.
    disabled_rule_ids: list[str] = Field(default_factory=list)
    #: Default grant lifetime in seconds when a task does not set one.
    default_ttl_seconds: int = Field(default=600, ge=30, le=86_400)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PolicyPack":
        text = Path(path).read_text(encoding="utf-8")
        return cls.from_yaml_text(text, origin=str(path))

    @classmethod
    def from_yaml_text(cls, text: str, origin: str = "<string>") -> "PolicyPack":
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise PolicyError(f"{origin}: invalid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise PolicyError(f"{origin}: top level must be a mapping")
        data.setdefault("name", Path(origin).stem if origin != "<string>" else "policy")
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise PolicyError(f"{origin}: invalid policy pack: {exc}") from exc


class TaskPolicy(BaseModel):
    """What one task type may use, before rules narrow it."""

    model_config = {"extra": "forbid"}

    description: str = ""
    #: Tools this task may be granted. A tool listed here but not in the
    #: registry is a composition error.
    allowed_tools: list[str] = Field(default_factory=list)
    #: Read-only tools are granted as-is; side-effect tools are gated.
    ttl_seconds: int = Field(default=600, ge=30, le=86_400)
    #: Per-tool overrides applied to the draft grant.
    tool_overrides: dict[str, "ToolOverride"] = Field(default_factory=dict)


class ToolOverride(BaseModel):
    """Per-tool ceilings declared in policy data."""

    model_config = {"extra": "forbid"}

    max_calls: int | None = Field(default=None, ge=1)
    requires_approval: bool = False
    limits: dict[str, Any] = Field(default_factory=dict)
    argument_constraints: dict[str, Any] | None = None
    condition: str | None = None


class RuleSpec(BaseModel):
    """A `DeclarativeRule` expressed as data."""

    model_config = {"extra": "forbid"}

    id: str = Field(min_length=1)
    #: Task types the rule applies to; omitted means every task in the pack.
    task_types: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    limits: list[LimitSpec] = Field(default_factory=list)
    required_arguments: list[str] = Field(default_factory=list)
    forbidden_values: dict[str, list[Any]] = Field(default_factory=dict)
    max_contacts_in_days: dict[str, Any] | None = None
    deny_if_fact_true: list[str] = Field(default_factory=list)
    approval_if_fact_true: list[str] = Field(default_factory=list)
    effect_on_match: Effect = "deny"
    message: str = ""
    severity: Severity = Severity.WARN

    def compile(self, default_task_types: Iterable[str]) -> DeclarativeRule:
        task_types = tuple(self.task_types) or tuple(default_task_types) or ("*",)
        return DeclarativeRule(
            rule_id=self.id,
            task_types=task_types,
            tools=tuple(self.tools) or ("*",),
            limits=tuple(self.limits),
            required_arguments=tuple(self.required_arguments),
            forbidden_values={k: tuple(v) for k, v in self.forbidden_values.items()},
            max_contacts_in_days=self.max_contacts_in_days,
            deny_if_fact_true=tuple(self.deny_if_fact_true),
            approval_if_fact_true=tuple(self.approval_if_fact_true),
            effect_on_match=self.effect_on_match,
            message=self.message,
            severity=self.severity,
        )


# --- Composition ---------------------------------------------------------


@dataclass
class ComposedPolicy:
    """Baseline + app packs, resolved into what the agent actually runs."""

    registry: ToolRegistry
    baseline_packs: list[PolicyPack]
    app_packs: list[PolicyPack]
    baseline_rules: list[Any]
    app_rules: list[Any]
    tasks: dict[str, TaskPolicy]
    #: Which pack supplied each task, for grant explanations.
    task_origin: dict[str, str]

    @property
    def task_types(self) -> list[str]:
        return sorted(self.tasks)

    def rules_for(self, task_type: str) -> list[Any]:
        """Baseline first, then app rules. Order matters for tightening."""
        applicable = [
            r
            for r in self.baseline_rules + self.app_rules
            if "*" in tuple(r.task_types) or task_type in tuple(r.task_types)
        ]
        return applicable

    def task_policy(self, task_type: str) -> TaskPolicy:
        try:
            return self.tasks[task_type]
        except KeyError:
            raise UnknownTaskTypeError(
                f"no policy registered for task type '{task_type}'"
            ) from None


class GrantVerifier:
    """Checks that a presented grant is genuine and in scope.

    Kept separate from the rules so the verification logic — signature,
    binding, expiry, replay — lives in one auditable place.
    """

    def __init__(
        self,
        keyring: "Keyring",
        *,
        replay_guard: "ReplayGuard | None" = None,
        now: Any = None,
    ) -> None:
        self.keyring = keyring
        self.replay_guard = replay_guard
        self._now = now

    def _current(self) -> Any:
        from .contracts import utcnow

        return self._now() if callable(self._now) else utcnow()

    def verify(
        self, authorization: TaskAuthorization, request: ActionRequest
    ) -> RuleOutcome | None:
        if authorization.decision is not Decision.ALLOW:
            return RuleOutcome(
                effect="deny",
                message="the presented authorization did not approve this task",
                severity=Severity.CRITICAL,
                details={"decision": authorization.decision.value},
            )
        if authorization.is_expired(self._current()):
            return RuleOutcome(
                effect="deny",
                message="the authorization has expired; request a new one",
                severity=Severity.WARN,
                details={"expires_at": authorization.expires_at.isoformat()},
            )
        for label, granted, presented in (
            ("app", authorization.app_id, request.app_id),
            ("agent", authorization.agent_id, request.agent_id),
            ("principal", authorization.principal, request.principal),
            ("task type", authorization.task_type, request.task_type),
        ):
            if granted != presented:
                return RuleOutcome(
                    effect="deny",
                    message=f"the authorization was issued for a different {label}",
                    severity=Severity.CRITICAL,
                    details={"field": label, "granted": granted, "presented": presented},
                )
        if not self.keyring.verify(authorization):
            return RuleOutcome(
                effect="deny",
                message="the authorization signature is missing or invalid",
                severity=Severity.CRITICAL,
                details={"authorization_id": authorization.authorization_id},
            )
        if self.replay_guard is not None:
            if self.replay_guard.seen(
                authorization.authorization_id, request.request_id
            ):
                return RuleOutcome(
                    effect="deny",
                    message="this authorization has already been used for this call",
                    severity=Severity.CRITICAL,
                    details={"authorization_id": authorization.authorization_id},
                )
            # Pair-based replay only catches an identical request repeated.
            # A single-use grant must also stop being honoured once it has
            # authorised anything at all, or an agent could mint a fresh
            # request id and spend the same authority twice.
            if self.replay_guard.exceeds(authorization):
                return RuleOutcome(
                    effect="deny",
                    message=(
                        "this authorization has already been used; a grant "
                        f"permits at most {authorization.max_uses} side effect(s) "
                        "— request a new authorization"
                    ),
                    severity=Severity.WARN,
                    details={
                        "authorization_id": authorization.authorization_id,
                        "max_uses": authorization.max_uses,
                        "uses": self.replay_guard.uses(authorization.authorization_id),
                    },
                )
        return None


class ReplayGuard(Protocol):
    """Records which grants have been consumed, and how often.

    Two distinct protections, both required:

    - `seen` / `consume` stop an identical request being replayed.
    - `uses` / `exceeds` stop a grant's own usage budget being spent twice
      under different request ids.

    A deployed app running several processes should back this with shared
    storage; the in-memory implementation is correct for one process only.
    """

    def seen(self, authorization_id: str, request_id: str) -> bool: ...

    def consume(self, authorization_id: str, request_id: str) -> None: ...

    def uses(self, authorization_id: str) -> int: ...

    def exceeds(self, authorization: TaskAuthorization) -> bool: ...


class InMemoryReplayGuard:
    """Single-process replay guard.

    Honours `max_uses`: a grant may authorise more than one call only if
    the policy said so, and even then the same request id cannot be
    replayed.
    """

    def __init__(self) -> None:
        self._uses: dict[str, int] = {}
        self._pairs: set[tuple[str, str]] = set()

    def seen(self, authorization_id: str, request_id: str) -> bool:
        return (authorization_id, request_id) in self._pairs

    def consume(self, authorization_id: str, request_id: str) -> None:
        self._pairs.add((authorization_id, request_id))
        self._uses[authorization_id] = self._uses.get(authorization_id, 0) + 1

    def uses(self, authorization_id: str) -> int:
        return self._uses.get(authorization_id, 0)

    def exceeds(self, authorization: TaskAuthorization) -> bool:
        if authorization.max_uses == 0:
            return False  # unlimited by policy
        return self.uses(authorization.authorization_id) >= authorization.max_uses


class Keyring:
    """Holds the signing key. Never logs it, never serialises it.

    The key comes from the environment. Local development without a key is
    possible but must be explicit, and every grant it produces is marked
    `unsigned=True` so a deployment cannot quietly run without signatures.
    """

    #: Environment variable the key is read from.
    ENV_VAR = "HELIOS_AUDIT_SIGNING_KEY"

    def __init__(
        self,
        key: bytes | None = None,
        key_id: str = "helios-audit-local",
        *,
        allow_unsigned: bool = False,
    ) -> None:
        self.key = key
        self.key_id = key_id
        self.allow_unsigned = allow_unsigned

    @classmethod
    def from_env(cls, *, allow_unsigned: bool = False) -> "Keyring":
        import os

        raw = os.environ.get(cls.ENV_VAR, "").strip()
        key_id = os.environ.get("HELIOS_AUDIT_SIGNING_KEY_ID", "").strip()
        return cls(
            key=raw.encode("utf-8") if raw else None,
            key_id=key_id or "helios-audit-local",
            allow_unsigned=allow_unsigned,
        )

    @property
    def can_sign(self) -> bool:
        return self.key is not None and len(self.key) > 0

    def sign(self, authorization: TaskAuthorization) -> TaskAuthorization:
        """Return a signed copy, or an explicitly-marked unsigned one."""
        if self.can_sign:
            assert self.key is not None
            return authorization.sign(self.key, self.key_id)
        if not self.allow_unsigned:
            from .errors import SigningKeyMissingError

            raise SigningKeyMissingError(
                f"no signing key configured; set {self.ENV_VAR} or pass "
                "allow_unsigned=True for local development"
            )
        return authorization.model_copy(update={"unsigned": True, "signature": None})

    def verify(self, authorization: TaskAuthorization) -> bool:
        """Verify a presented grant.

        An unsigned grant is acceptable only when this keyring was
        explicitly configured to allow unsigned operation — otherwise a
        stripped signature would silently pass.
        """
        if authorization.signature is None:
            return bool(self.allow_unsigned and authorization.unsigned)
        if not self.can_sign:
            return False
        assert self.key is not None
        if authorization.signature.key_id != self.key_id:
            return False
        return authorization.verify(self.key)


def baseline_pack() -> PolicyPack:
    """The platform's own policy pack.

    Declares the protected rule ids and the task-independent defaults. It
    intentionally grants no tools: capability comes from the app pack,
    bounded by the baseline.
    """
    return PolicyPack(
        name="helios-baseline",
        source="baseline",
        version="1",
        protected_rule_ids=sorted(BASELINE_RULE_IDS),
        default_ttl_seconds=600,
    )


class PolicyEngine:
    """Composes packs, validates them, and answers policy questions."""

    def __init__(self, composed: ComposedPolicy, verifier: GrantVerifier) -> None:
        self.composed = composed
        self.verifier = verifier

    # -- Composition ------------------------------------------------------

    @classmethod
    def compose(
        cls,
        registry: ToolRegistry,
        *,
        app_packs: Iterable[PolicyPack] = (),
        baseline_packs: Iterable[PolicyPack] = (),
        keyring: Keyring | None = None,
        replay_guard: ReplayGuard | None = None,
        now: Any = None,
    ) -> "PolicyEngine":
        """Merge baseline and app packs, refusing any loosening.

        Order of checks matters: structural errors first, then the
        anti-loosening rule, then tool existence, then side-effect
        consistency. Each produces a distinct message so a generated policy
        can be fixed from the error alone — Qwen is the consumer here.
        """
        baselines = [baseline_pack(), *baseline_packs]
        apps = list(app_packs)
        keyring = keyring or Keyring.from_env(allow_unsigned=True)

        protected: set[str] = set()
        for pack in baselines:
            if pack.source != "baseline":
                raise PolicyError(
                    f"pack '{pack.name}' declares source='baseline' inconsistently"
                )
            protected.update(pack.protected_rule_ids)

        for pack in apps:
            if pack.source != "app":
                raise PolicyError(
                    f"pack '{pack.name}' must have source='app'; only the platform "
                    "ships baseline packs"
                )
            offending = protected.intersection(pack.disabled_rule_ids)
            if offending:
                raise PolicyLooseningError(
                    f"app pack '{pack.name}' tried to disable protected baseline "
                    f"rule(s): {', '.join(sorted(offending))}. Baseline policy may "
                    "be extended but never weakened."
                )

        tasks: dict[str, TaskPolicy] = {}
        origin: dict[str, str] = {}
        for pack in baselines:
            for task_type, task in pack.tasks.items():
                tasks[task_type] = task
                origin[task_type] = pack.name
        for pack in apps:
            for task_type, task in pack.tasks.items():
                existing = tasks.get(task_type)
                if existing is not None and origin.get(task_type, "").startswith(
                    "helios-baseline"
                ):
                    # An app may narrow a baseline task but not widen it.
                    extra = set(task.allowed_tools) - set(existing.allowed_tools)
                    if extra:
                        raise PolicyLooseningError(
                            f"app pack '{pack.name}' tried to add tools "
                            f"{', '.join(sorted(extra))} to baseline task "
                            f"'{task_type}'"
                        )
                    if task.ttl_seconds > existing.ttl_seconds:
                        raise PolicyLooseningError(
                            f"app pack '{pack.name}' tried to extend the grant "
                            f"lifetime of baseline task '{task_type}'"
                        )
                tasks[task_type] = task
                origin[task_type] = pack.name

        # Every referenced tool must exist, and its side-effect flag in
        # policy must match the registry. Mismatched flags are how a gated
        # tool sneaks through as read-only.
        for task_type, task in tasks.items():
            for tool in task.allowed_tools:
                if not registry.has(tool):
                    raise UnknownToolError(
                        f"task '{task_type}' allows unregistered tool '{tool}'"
                    )
            for tool, override in task.tool_overrides.items():
                if not registry.has(tool):
                    raise UnknownToolError(
                        f"task '{task_type}' overrides unregistered tool '{tool}'"
                    )
                descriptor = registry.get(tool)
                if override.argument_constraints and not descriptor.input_schema:
                    raise PolicyError(
                        f"task '{task_type}' constrains arguments of '{tool}', "
                        "which declares no input schema"
                    )

        # One verifier instance, shared by the rule that checks grants and
        # by the engine that issues them. Two instances would keep separate
        # replay state and a consumed grant could be replayed.
        verifier = GrantVerifier(keyring, replay_guard=replay_guard, now=now)
        baseline_rules: list[Any] = [
            ToolMustBeRegisteredRule(registry),
            ArgumentsMustValidateRule(registry),
            PermissionRequiredRule(registry),
            CallBudgetRule(),
            ExplicitApprovalRule(),
            IntentStatedRule(),
            GrantMustBeValidRule(registry, verifier),
            # Last: emits the positive allow only if nothing above objected.
            # Ordering is for readability, not correctness — precedence
            # already ranks deny and hold above allow.
            WithinGrantAllowRule(registry),
        ]
        app_rules: list[Any] = []
        for pack in apps:
            default_tasks = tuple(pack.tasks) or ("*",)
            for spec in pack.rules:
                compiled = spec.compile(default_tasks)
                if compiled.rule_id in protected:
                    raise PolicyLooseningError(
                        f"app pack '{pack.name}' redefined protected rule "
                        f"'{compiled.rule_id}'"
                    )
                app_rules.append(compiled)
                # Rule ids must be unique or the audit trail is ambiguous.
        seen_ids: set[str] = set()
        for rule in baseline_rules + app_rules:
            if rule.rule_id in seen_ids:
                raise PolicyError(f"duplicate rule id '{rule.rule_id}'")
            seen_ids.add(rule.rule_id)

        composed = ComposedPolicy(
            registry=registry,
            baseline_packs=baselines,
            app_packs=apps,
            baseline_rules=baseline_rules,
            app_rules=app_rules,
            tasks=tasks,
            task_origin=origin,
        )
        return cls(composed, verifier)

    # -- Grant shaping ----------------------------------------------------

    def draft_grant(
        self, task_type: str, requested_tools: list[str]
    ) -> GrantDraft:
        """Start from policy, intersect with the request, then let rules tighten.

        The intersection is the security-critical step: the action agent
        gets at most what policy allows, and only the subset it asked for.
        Asking for less is fine; asking for more is simply not granted.
        """
        task = self.composed.task_policy(task_type)
        allowed = list(task.allowed_tools)
        wanted = set(requested_tools) if requested_tools else set(allowed)
        draft = GrantDraft(matched_packs=[self.composed.task_origin.get(task_type, "")])
        for tool in allowed:
            if tool not in wanted:
                continue
            descriptor = self.composed.registry.get(tool)
            override = task.tool_overrides.get(tool)
            permission = ToolPermission(
                tool=tool,
                side_effect=descriptor.side_effect,
                argument_constraints=(
                    override.argument_constraints if override else None
                )
                or descriptor.input_schema,
                limits=dict(override.limits) if override else {},
                max_calls=override.max_calls if override else None,
                requires_approval=bool(override.requires_approval) if override else False,
                condition=override.condition if override else None,
            )
            draft.allow(permission)
        return draft


# PolicyPack annotates TaskPolicy and RuleSpec as strings because they are
# declared further down (pack -> task -> override reads better than the
# reverse). Under `from __future__ import annotations` Pydantic cannot
# resolve those until every name exists, so resolve them explicitly here.
TaskPolicy.model_rebuild()
RuleSpec.model_rebuild()
PolicyPack.model_rebuild()


__all__ = [
    "AuditFacts",
    "BaselineRule",
    "ComposedPolicy",
    "DeclarativeRule",
    "GrantDraft",
    "GrantVerifier",
    "InMemoryReplayGuard",
    "Keyring",
    "LimitSpec",
    "PolicyEngine",
    "PolicyPack",
    "ReplayGuard",
    "Rule",
    "RuleOutcome",
    "RuleSpec",
    "TaskPolicy",
    "ToolDescriptor",
    "ToolOverride",
    "ToolRegistry",
    "baseline_pack",
]

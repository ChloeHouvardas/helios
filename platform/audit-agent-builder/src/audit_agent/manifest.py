"""The audit manifest: how Qwen creates an audit agent from data alone.

`specs/decisions.md` fixes the principle — app-specific agent behaviour is
data in a manifest, and Qwen generates the manifest rather than writing
agent logic. For the audit side that means an app's entire authority model
is declared, validated and reviewable without reading Python.

A manifest declares four things:

1. **tools** — what exists, and which of it has side effects. This is the
   audit agent's registry, so it decides what can be granted at all.
2. **tasks** — what an action agent may be asked to do, and which tools
   each task may use. This is the direct answer to "what tools can the
   action agent use for this task".
3. **rules** — the limits, expressed declaratively: caps, contact
   frequency, forbidden values, facts that force owner approval.
4. **settings** — grant lifetime, signing, advisory review, log location.

Validation is deliberately strict and its errors are written for a model to
act on. A generated manifest that would silently weaken the platform is
rejected with a specific message, because the alternative — an unenforced
control that looks enforced — is the worst possible failure in a financial
audit system.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .contracts import Effect, Severity
from .errors import PolicyError, PolicyLooseningError, UnknownToolError
from .policy import (
    BASELINE_RULE_IDS,
    LimitSpec,
    PolicyPack,
    RuleSpec,
    TaskPolicy,
    ToolDescriptor,
    ToolOverride,
    ToolRegistry,
)


class ManifestTool(BaseModel):
    """One tool the audit agent knows about."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    #: The single most important flag in the manifest. True means every
    #: call becomes a gated proposal; False means it runs directly.
    side_effect: bool = False
    #: JSON Schema for arguments. Strongly recommended for side-effect
    #: tools: without it, malformed arguments cannot be caught before the
    #: call happens.
    input_schema: dict[str, Any] | None = None
    owner: str = "app"


class ManifestTask(BaseModel):
    """One task type an action agent may be authorized for."""

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    #: The allowlist. A tool absent from here cannot be granted for this
    #: task no matter what the action agent asks for.
    allowed_tools: list[str] = Field(min_length=1)
    #: How long a grant stays valid. Short by default: a grant is authority
    #: to act now, not a standing capability.
    ttl_seconds: int = Field(default=300, ge=30, le=86_400)
    tool_overrides: dict[str, ToolOverride] = Field(default_factory=dict)


class ManifestRule(BaseModel):
    """A limit, expressed as data."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    description: str = ""
    #: Omitted means the rule applies to every task in the manifest.
    task_types: list[str] = Field(default_factory=list)
    #: Omitted means every tool.
    tools: list[str] = Field(default_factory=list)
    limits: list[LimitSpec] = Field(default_factory=list)
    required_arguments: list[str] = Field(default_factory=list)
    forbidden_values: dict[str, list[Any]] = Field(default_factory=dict)
    max_contacts_in_days: dict[str, Any] | None = None
    #: Trusted facts that force a deny (e.g. changed supplier bank details).
    deny_if_fact_true: list[str] = Field(default_factory=list)
    #: Trusted facts that force owner approval (e.g. an outlier amount).
    approval_if_fact_true: list[str] = Field(default_factory=list)
    message: str = ""
    severity: Severity = Severity.WARN

    @field_validator("id")
    @classmethod
    def _not_protected(cls, value: str) -> str:
        """An app may not reuse a baseline rule id.

        Redefining `baseline.grant_valid` as something permissive would be
        the single most damaging thing a generated manifest could do, so it
        is refused at parse time rather than at composition time.
        """
        if value in BASELINE_RULE_IDS:
            raise PolicyLooseningError(
                f"rule id '{value}' is reserved for the platform baseline and "
                "cannot be redefined; choose an app-specific id such as "
                f"'app.{value.split('.', 1)[-1]}'"
            )
        if value.startswith("baseline."):
            raise PolicyLooseningError(
                f"rule ids beginning with 'baseline.' are reserved by the "
                f"platform (got '{value}')"
            )
        return value

    def to_rule_spec(self) -> RuleSpec:
        return RuleSpec(
            id=self.id,
            task_types=self.task_types,
            tools=self.tools,
            limits=self.limits,
            required_arguments=self.required_arguments,
            forbidden_values=self.forbidden_values,
            max_contacts_in_days=self.max_contacts_in_days,
            deny_if_fact_true=self.deny_if_fact_true,
            approval_if_fact_true=self.approval_if_fact_true,
            message=self.message,
            severity=self.severity,
        )


class ManifestSettings(BaseModel):
    """Runtime settings for the generated audit agent."""

    model_config = ConfigDict(extra="forbid")

    #: Default grant lifetime when a task does not set one.
    default_ttl_seconds: int = Field(default=300, ge=30, le=86_400)
    #: Window used for contact-frequency facts.
    contact_window_days: int = Field(default=7, ge=1, le=365)
    #: Where the append-only log is written. Relative paths resolve against
    #: the app's data directory at runtime.
    log_path: str = "audit-log.jsonl"
    #: Advisory reviewers. `heuristic` is deterministic and always available;
    #: `qwen` adds model commentary and is non-binding either way.
    advisory: list[Literal["none", "heuristic", "qwen"]] = Field(
        default_factory=lambda: ["heuristic"]
    )
    #: Model used when `qwen` advisory is enabled.
    advisory_model: str = "qwen-plus"
    #: Local development only: allow issuing grants without a signing key.
    #: Every such grant is marked unsigned, and a strict auditor rejects it.
    allow_unsigned_grants: bool = False

    @field_validator("advisory")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class AuditManifest(BaseModel):
    """A complete audit agent definition, as data."""

    model_config = ConfigDict(extra="forbid")

    #: Bumped if the manifest shape changes incompatibly.
    manifest_version: Literal["1"] = "1"
    app_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    tools: list[ManifestTool] = Field(min_length=1)
    tasks: dict[str, ManifestTask] = Field(min_length=1)
    rules: list[ManifestRule] = Field(default_factory=list)
    settings: ManifestSettings = Field(default_factory=ManifestSettings)

    # -- Loading ----------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "AuditManifest":
        """Load and validate a manifest from YAML or JSON."""
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        return cls.parse(text, origin=str(path))

    @classmethod
    def parse(cls, text: str, origin: str = "<string>") -> "AuditManifest":
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise PolicyError(f"{origin}: invalid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise PolicyError(f"{origin}: a manifest must be a mapping")
        try:
            manifest = cls.model_validate(data)
        except ValidationError as exc:
            raise PolicyError(
                f"{origin}: invalid audit manifest:\n{format_validation_error(exc)}"
            ) from exc
        manifest.self_check(origin=origin)
        return manifest

    # -- Validation -------------------------------------------------------

    def self_check(self, origin: str = "<manifest>") -> None:
        """Cross-field checks that a schema cannot express.

        Each check produces one specific, actionable message. They exist so
        a generated manifest is either correct or loudly wrong — never
        quietly permissive.
        """
        tool_names = [t.name for t in self.tools]
        duplicates = {n for n in tool_names if tool_names.count(n) > 1}
        if duplicates:
            raise PolicyError(
                f"{origin}: tool declared more than once: "
                f"{', '.join(sorted(duplicates))}"
            )
        known = set(tool_names)

        for task_type, task in self.tasks.items():
            unknown = [t for t in task.allowed_tools if t not in known]
            if unknown:
                raise UnknownToolError(
                    f"{origin}: task '{task_type}' allows tools that are not "
                    f"declared: {', '.join(unknown)}. Declare them under "
                    "'tools' or remove them."
                )
            for tool in task.tool_overrides:
                if tool not in known:
                    raise UnknownToolError(
                        f"{origin}: task '{task_type}' overrides an undeclared "
                        f"tool '{tool}'"
                    )
                if tool not in task.allowed_tools:
                    raise PolicyError(
                        f"{origin}: task '{task_type}' overrides tool '{tool}' "
                        "which it does not allow; the override would never apply"
                    )

        declared = {t.name: t for t in self.tools}
        for rule in self.rules:
            for tool in rule.tools:
                if tool not in known:
                    raise UnknownToolError(
                        f"{origin}: rule '{rule.id}' targets an undeclared tool "
                        f"'{tool}'"
                    )
            for task_type in rule.task_types:
                if task_type not in self.tasks:
                    raise PolicyError(
                        f"{origin}: rule '{rule.id}' targets an undeclared task "
                        f"type '{task_type}'"
                    )
            if not rule.limits and not rule.required_arguments and not rule.forbidden_values \
                    and not rule.max_contacts_in_days and not rule.deny_if_fact_true \
                    and not rule.approval_if_fact_true:
                raise PolicyError(
                    f"{origin}: rule '{rule.id}' declares no check. A rule must "
                    "set at least one of: limits, required_arguments, "
                    "forbidden_values, max_contacts_in_days, "
                    "deny_if_fact_true, approval_if_fact_true."
                )
            for limit in rule.limits:
                if not limit.argument and not limit.fact:
                    raise PolicyError(
                        f"{origin}: rule '{rule.id}' has a limit that names "
                        "neither 'argument' nor 'fact', so it can never be "
                        "evaluated"
                    )
                if limit.clamp and not limit.argument:
                    raise PolicyError(
                        f"{origin}: rule '{rule.id}' sets clamp=true on a fact "
                        "limit. Only argument limits can be clamped, because a "
                        "fact is observed, not chosen by the agent."
                    )
                if limit.clamp and limit.on_exceed == "deny":
                    raise PolicyError(
                        f"{origin}: rule '{rule.id}' sets both clamp and "
                        "on_exceed=deny; clamping already resolves the "
                        "violation, so the two contradict each other"
                    )
                # Argument limits must be checkable: the tool needs a schema,
                # or the argument is unconstrained and the limit is theatre.
                for tool in rule.tools or list(known):
                    descriptor = declared.get(tool)
                    if descriptor is None or not limit.argument:
                        continue
                    schema = descriptor.input_schema or {}
                    properties = schema.get("properties") or {}
                    root = limit.argument.split(".")[0]
                    if properties and root not in properties:
                        raise PolicyError(
                            f"{origin}: rule '{rule.id}' limits argument "
                            f"'{limit.argument}', but tool '{tool}' has no such "
                            "argument in its input schema"
                        )

        # A side-effect tool with no schema cannot have its arguments
        # validated, so malformed calls would reach execution unchecked.
        for tool in self.tools:
            if tool.side_effect and not tool.input_schema:
                raise PolicyError(
                    f"{origin}: tool '{tool.name}' has side effects but declares "
                    "no input_schema. Without a schema the audit agent cannot "
                    "reject malformed arguments before the action runs."
                )

        self._check_conflicting_limit_strategies(origin)

    def _check_conflicting_limit_strategies(self, origin: str) -> None:
        """Refuse a clamp and a hold/deny on the same field.

        This is the single most likely mistake in a generated manifest, and it
        fails in a confusing way rather than a loud one. A generated author
        writes a cap rule and then an approval rule for the same field,
        expecting the cap to reduce the value before the approval rule sees
        it. It does not: every rule evaluates the agent's original request
        independently, so the hold always wins and the clamp is dead config.

        Order-independence between rules is the correct design — a control's
        verdict must not depend on where it sits in a file. But that makes
        this combination meaningless, so it is rejected with an explanation
        of what to write instead.
        """
        # (tool, bound field) -> list of (rule id, strategy)
        seen: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for rule in self.rules:
            tools = rule.tools or [t.name for t in self.tools]
            for limit in rule.limits:
                bound = limit.argument or limit.fact
                if not bound:
                    continue
                strategy = "clamp" if limit.clamp else limit.on_exceed
                for tool in tools:
                    seen.setdefault((tool, bound), []).append((rule.id, strategy))

        for (tool, bound), entries in seen.items():
            strategies = {strategy for _, strategy in entries}
            if "clamp" in strategies and strategies - {"clamp"}:
                clamping = sorted(rid for rid, s in entries if s == "clamp")
                blocking = sorted(
                    rid for rid, s in entries if s != "clamp"
                )
                raise PolicyError(
                    f"{origin}: conflicting policies on '{bound}' for tool "
                    f"'{tool}': {', '.join(clamping)} clamp(s) the value while "
                    f"{', '.join(blocking)} hold(s) or deny(s) it. Rules do not "
                    "see each other's clamps — each evaluates the agent's "
                    "original request — so the clamp would never take effect. "
                    "Pick one policy: use clamp to allow a reduced value without "
                    "a human, or on_exceed: hold/deny to route it to the owner."
                )

    # -- Conversion -------------------------------------------------------

    def tool_registry(self) -> ToolRegistry:
        """Build the registry this manifest describes."""
        return ToolRegistry(
            ToolDescriptor(
                name=t.name,
                description=t.description,
                side_effect=t.side_effect,
                input_schema=t.input_schema,
                owner=t.owner,
            )
            for t in self.tools
        )

    def policy_pack(self) -> PolicyPack:
        """Build the app policy pack this manifest describes.

        Source is forced to `app`: a manifest can never produce a baseline
        pack, which is the second half of "apps may extend but not weaken".
        """
        return PolicyPack(
            name=f"{self.app_id}-audit",
            source="app",
            version=str(self.manifest_version),
            tasks={
                task_type: TaskPolicy(
                    description=task.description,
                    allowed_tools=list(task.allowed_tools),
                    ttl_seconds=task.ttl_seconds,
                    tool_overrides=dict(task.tool_overrides),
                )
                for task_type, task in self.tasks.items()
            },
            rules=[rule.to_rule_spec() for rule in self.rules],
            default_ttl_seconds=self.settings.default_ttl_seconds,
        )

    def summary(self) -> str:
        """Human-readable description of the authority this manifest grants.

        Intended for the owner and for a reviewer: it states, in plain
        language, what an action agent will be allowed to do.
        """
        gated = [t.name for t in self.tools if t.side_effect]
        direct = [t.name for t in self.tools if not t.side_effect]
        lines = [
            f"Audit agent for '{self.app_id}' ({self.name})",
            f"Tasks: {', '.join(sorted(self.tasks))}",
            f"Gated tools (need per-call review): {', '.join(sorted(gated)) or '(none)'}",
            f"Direct tools (read-only): {', '.join(sorted(direct)) or '(none)'}",
            f"Rules: {len(self.rules)} app rule(s) + {len(BASELINE_RULE_IDS)} platform baseline rule(s)",
        ]
        for task_type, task in sorted(self.tasks.items()):
            lines.append(
                f"  {task_type}: {', '.join(task.allowed_tools)} "
                f"(grant lasts {task.ttl_seconds}s)"
            )
        return "\n".join(lines)


def format_validation_error(exc: ValidationError) -> str:
    """Render a Pydantic error as a list a model can act on.

    Raw Pydantic output includes Python type reprs and input echoes, which
    are noisy and can leak context. This keeps location, message and the
    offending value only.
    """
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        message = error["msg"]
        lines.append(f"- {location}: {message}")
    return "\n".join(lines)


def validate_manifest_text(text: str, origin: str = "<string>") -> tuple[bool, list[str]]:
    """Check a manifest without building anything.

    Returns (ok, problems). This is the entry point a build pipeline calls
    on generated output: cheap, side-effect free, and its messages are the
    feedback loop back to Qwen.
    """
    try:
        AuditManifest.parse(text, origin=origin)
    except (PolicyError, PolicyLooseningError, UnknownToolError) as exc:
        return False, [str(exc)]
    except Exception as exc:  # pragma: no cover - unexpected shape
        return False, [f"{type(exc).__name__}: {exc}"]
    return True, []


def validate_manifest_file(path: str | Path) -> tuple[bool, list[str]]:
    """`validate_manifest_text` for a file on disk."""
    path = Path(path)
    if not path.exists():
        return False, [f"{path}: no such file"]
    return validate_manifest_text(path.read_text(encoding="utf-8"), origin=str(path))


__all__ = [
    "AuditManifest",
    "ManifestRule",
    "ManifestSettings",
    "ManifestTask",
    "ManifestTool",
    "format_validation_error",
    "validate_manifest_file",
    "validate_manifest_text",
]

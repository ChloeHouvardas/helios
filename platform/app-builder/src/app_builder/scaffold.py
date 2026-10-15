"""Turn a validated spec into a complete application skeleton.

    helios-app new <app-dir> --spec helios-app.yaml [--intake intake.yaml]

The contract, in the order a reader is likely to need it:

* **Reads the spec only.** It never imports the agent builders. Their manifests are
  read as plain data and generated as plain data, because the generated app runs
  its own copy of the shared runtime rather than reaching back into the platform.
* **Never overwrites.** An existing file is reported as `skipped: <path> (exists)`
  and left alone. Regenerating over someone's in-progress work is the kind of
  failure that costs hours and produces no error message.
* **Idempotent.** Running twice on the same spec writes nothing the second time.
  A revision loop — edit the spec, regenerate — is the normal way this is used.

What "complete" means here
--------------------------
The skeleton validates clean: `validate_app` reports 0 errors on it immediately
after generation, without the caller writing anything. The policy is deliberately
NOT complete: the audit manifest ships with the five rule shapes as a commented
TODO block, because policy is the product and guessing it would produce an app
that looks governed and is not.

Why the generated policy is a comment and not a default rule
------------------------------------------------------------
A generated rule with a made-up ceiling is worse than no rule. It passes
validation, it reads like a control, and it caps the wrong thing — so the owner
believes the app is governed when the number came from a generator. Everything the
generator cannot know is left as a TODO naming what to write.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

import yaml

from .deployment import ManifestFields, discover_manifest_fields
from .schema import ddl_for, store_module_for
from .spec import AppSpec, AppSpecError
from .stacks import Stack, StacksError, stack_for_spec
from .validate import SUBJECT_KEYS

__all__ = [
    "ScaffoldError", "ScaffoldResult", "scaffold", "guess_side_effect",
    "SIDE_EFFECT_VERBS",
]


class ScaffoldError(RuntimeError):
    """The spec cannot be scaffolded. Raised, not returned: nothing is written."""


class ScaffoldResult:
    """What a scaffold run did.

    `written` and `skipped` are the two lists a caller reports to the user;
    `warnings` are generation-time advisories (a published entity that is not a
    local one, a read with no consumer) that do not stop the run.
    """

    def __init__(self, app_dir: Path, *, dry_run: bool = False) -> None:
        self.app_dir = app_dir
        self.dry_run = dry_run
        self.written: list[Path] = []
        self.skipped: list[Path] = []
        self.warnings: list[str] = []

    def report(self) -> str:
        lines: list[str] = []
        verb = "would write" if self.dry_run else "wrote"
        for path in self.written:
            lines.append(f"{verb}: {self._rel(path)}")
        for path in self.skipped:
            lines.append(f"skipped: {self._rel(path)} (exists)")
        for warning in self.warnings:
            lines.append(f"warning: {warning}")
        return "\n".join(lines)

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.app_dir))
        except ValueError:
            return str(path)


#: A tool whose name carries one of these verbs has a side effect: it changes
#: something outside the agent's own view of the world. Everything else is a read.
#:
#: Guessing is deliberate and marked for review rather than silently wrong: the
#: scaffolder cannot know whether `email.draft` sends anything, so it writes its
#: best guess and puts `# REVIEW` next to it. A wrong guess in the unsafe direction
#: — a real side effect declared read-only — is caught by the validator once
#: `tools.py` is filled in, because the two files must agree.
SIDE_EFFECT_VERBS = frozenset({
    "send", "pay", "publish", "save", "create", "record", "reschedule", "refund",
})


def guess_side_effect(tool_name: str) -> bool:
    """True when the tool's name says it changes something.

    Only the segment that carries the OPERATION is considered, and which segment
    that is depends on the naming style:

      * namespaced (`payments.send`) — the operation is the LAST segment, because
        the earlier ones name the subsystem;
      * snake_case (`record_payment`, `read_record`) — the operation is the FIRST
        segment, because the later ones name the object.

    Checking every segment instead is wrong in the unsafe-looking direction:
    `read_record` contains the segment `record`, which is a side-effect verb, so a
    read would be declared gated and — if the reverse case occurred — a genuine
    write could be waved through as a read. Both real apps' flags come out exactly
    right under this rule, which is the only evidence that it is the right rule.
    """
    lowered = tool_name.strip().lower()
    if not lowered:
        return False
    if "." in lowered:
        segments = [s for s in lowered.split(".") if s]
        operation = segments[-1] if segments else ""
    else:
        operation = lowered.split("_", 1)[0]
    return operation in SIDE_EFFECT_VERBS


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def scaffold(app_dir: Path | str, spec: AppSpec, *, dry_run: bool = False,
             intake: dict[str, Any] | None = None,
             spec_path: Path | str | None = None,
             repo_root: Path | str | None = None) -> ScaffoldResult:
    """Generate an application skeleton from a validated spec.

    Nothing is written until every generator has produced its text, so a failure
    part-way through cannot leave a half-generated app behind. That matters more
    than it sounds: a partially generated app passes some checks and fails others,
    and the failure looks like a bug in the app rather than an aborted run.
    """
    app_dir = Path(app_dir).resolve()
    result = ScaffoldResult(app_dir, dry_run=dry_run)

    try:
        stack = stack_for_spec(spec)
    except StacksError as exc:
        raise ScaffoldError(f"cannot scaffold {spec.app_id}: {exc}") from exc

    if not spec.tools:
        raise ScaffoldError(
            f"cannot scaffold {spec.app_id}: the spec declares no tools, and an "
            "audit manifest with no tools cannot load. Add at least one tool to "
            "the spec's top-level `tools` list — a read-only lookup is enough to "
            "start.")

    fields = discover_manifest_fields(repo_root)

    generators = [
        # The spec itself. A generated app is validated as a directory containing
        # helios-app.yaml, and the validator resolves both manifest paths relative
        # to it — so without this file the skeleton cannot be validated at all, and
        # a later revision loop has nothing to regenerate from.
        ("helios-app.yaml", lambda: _spec_text(spec, spec_path)),
        ("helios.yaml", lambda: deployment_manifest(spec, fields)),
        (f"agents/{spec.app_id}-audit.yaml", lambda: audit_manifest(spec)),
        (f"agents/{spec.app_id}-action.json", lambda: action_manifest(spec)),
        ("facts.py", lambda: facts_module(spec)),
        ("tools.py", lambda: tools_module(spec)),
        ("graph.py", lambda: graph_module(spec, result)),
        ("store.py", lambda: store_module_for(spec, stack)),
        ("app.py", lambda: app_module(spec)),
        ("server.py", lambda: server_module(spec)),
        ("data/schema.sql", lambda: ddl_for(spec, stack)),
        ("data/README.md", lambda: data_readme(spec)),
        ("tests/test_controls.py", lambda: controls_tests(spec)),
        ("README.md", lambda: app_readme(spec)),
    ]

    rendered: list[tuple[Path, str]] = []
    for rel, generate in generators:
        rendered.append((app_dir / rel, generate()))

    rendered.extend(_kit_files(app_dir, spec, stack))

    for path, text in rendered:
        _write(path, text, result)

    return result


def _write(path: Path, text: str, result: ScaffoldResult) -> None:
    """Write one file, or report that it already exists. Never overwrite."""
    if path.exists():
        result.skipped.append(path)
        return
    result.written.append(path)
    if result.dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _spec_text(spec: AppSpec, spec_path: Path | str | None) -> str:
    """The spec text to place in the generated app.

    Copies the source file verbatim when it is known, because a spec carries the
    author's comments — the reasoning behind a data model, a note about why an
    entity is integrated rather than local — and re-dumping the model would destroy
    all of it. Falling back to a dump only when there is no source file (a spec
    built in memory, e.g. from `spec-from-intake`).
    """
    if spec_path is not None:
        path = Path(spec_path)
        if path.is_file():
            return path.read_text(encoding="utf-8")
    # `mode="json"` is required, not cosmetic: the model's closed sets are StrEnum
    # members, and yaml.safe_dump raises RepresenterError on an enum instance. In
    # JSON mode pydantic emits the plain string values, which is what the spec file
    # is supposed to contain.
    return yaml.safe_dump(spec.model_dump(mode="json", exclude_none=False),
                          sort_keys=False, allow_unicode=True,
                          default_flow_style=False)


# ---------------------------------------------------------------------------
# helios.yaml — the deployment manifest
# ---------------------------------------------------------------------------

#: Tool-name prefix -> deployment permission. The engine's `ALLOWED_TOOLS` is a
#: closed set and `load_manifest` raises `ContractError` on anything else, so a
#: family with no mapping contributes nothing rather than making the app
#: undeployable. `discover_manifest_fields` reads the real set at generation time
#: and filters this again, so widening the engine widens what is emitted.
_PERMISSION_BY_FAMILY = {
    "payments": "payments",
    "email": "email",
    "graph": "knowledge-graph",
    "knowledge": "knowledge-graph",
    "kg": "knowledge-graph",
    "events": "events",
    "event": "events",
    # Families the engine does not know yet. They are mapped to the closest
    # existing permission rather than emitted as-is: an unknown permission is a
    # hard ContractError at deploy time, while a missing one only means the audit
    # sidecar does not pre-authorise that family.
    "forecast": None,
    "market": None,
    "spreadsheet": None,
    "collect": None,
    "behavior": None,
}


def permissions_for_tools(tools: Iterable[str], fields: ManifestFields) -> list[str]:
    """The permissions a tool list needs, restricted to what the engine accepts."""
    wanted: set[str] = set()
    for tool in tools:
        family = re.split(r"[._]", tool.strip())[0].lower()
        permission = _PERMISSION_BY_FAMILY.get(family)
        if permission:
            wanted.add(permission)
    # An app that publishes to the graph needs the permission even when no tool
    # name says so — the generated graph.py writes verified nodes.
    return sorted(w for w in wanted if w in fields.allowed_tools)


def deployment_manifest(spec: AppSpec, fields: ManifestFields) -> str:
    """`helios.yaml`, emitting only fields the engine actually accepts.

    The engine's model does not forbid extra keys, so an unrecognised one is
    dropped without an error — producing a manifest that looks configured and is
    not. `audit_manifest`, `data_dir` and `env` are therefore emitted only if the
    engine declares them, which today it does not.
    """
    permissions = permissions_for_tools(spec.tools, fields)
    document: dict[str, Any] = {"name": spec.app_id}

    if fields.accepts("tenant"):
        document["tenant"] = ""
    if fields.accepts("entry"):
        document["entry"] = "server:app"
    if fields.accepts("port"):
        document["port"] = 3000
    if fields.accepts("permissions"):
        document["permissions"] = permissions

    # Only emitted if the engine has grown these fields. Writing them today would
    # be silently dropped, which is worse than not writing them.
    if fields.accepts("audit_manifest"):
        document["audit_manifest"] = f"agents/{spec.app_id}-audit.yaml"
    if fields.accepts("data_dir"):
        document["data_dir"] = "/data"
    if fields.accepts("env"):
        document["env"] = [
            "HELIOS_AUDIT_SIGNING_KEY",
            "DASHSCOPE_API_KEY",
        ]

    header = (
        "# Deployment manifest for the Helios engine.\n"
        "#\n"
        f"# Field set: {fields.source}.\n"
        "# The engine's manifest model does not forbid unknown keys, so a field it\n"
        "# does not declare is dropped WITHOUT AN ERROR. Only accepted fields are\n"
        "# emitted here, which is why this file looks shorter than the docs suggest.\n"
    )
    if not fields.discovered:
        header += (
            "#\n"
            "# REVIEW: platform/deployment/engine/contracts.py was not readable at\n"
            "# generation time. Re-run `helios-app new` once it lands so this file\n"
            "# is regenerated against the engine's real field set.\n"
        )
    if permissions != _all_wanted(spec.tools):
        unmapped = sorted(_all_wanted(spec.tools) - set(permissions))
        if unmapped:
            header += (
                "#\n"
                f"# REVIEW: tool families {unmapped} have no permission the engine\n"
                "# accepts, so they are not listed. An unknown permission is a hard\n"
                "# ContractError at deploy time, which is worse than an absent one.\n"
            )

    body = yaml.safe_dump(document, sort_keys=False, allow_unicode=True,
                          default_flow_style=False)
    return header + "\n" + body


def _all_wanted(tools: Iterable[str]) -> set[str]:
    """Every permission a tool list would want, before engine filtering."""
    out: set[str] = set()
    for tool in tools:
        family = re.split(r"[._]", tool.strip())[0].lower()
        permission = _PERMISSION_BY_FAMILY.get(family)
        if permission:
            out.add(permission)
    return out


# ---------------------------------------------------------------------------
# agents/<id>-audit.yaml — the authoritative in-app gate
# ---------------------------------------------------------------------------

#: The five rule shapes, as a commented block. Exact YAML keys, because a rule
#: with a misspelt key is rejected by the loader and a rule with a *renamed* key
#: silently does not match.
_RULE_TODO_BLOCK = """\
# TODO: write this app's policy. This is the product, not the scaffolding.
#
# Five shapes, with the exact keys the loader accepts. Uncomment and fill in the
# ones this app needs; delete the rest. A rule id may NOT start with `baseline.`
# — those ids belong to the platform and redefining one is refused at load.
#
# Every fact key named below must be returned by facts.py's extra_facts_for, and
# every argument must exist in that tool's input_schema. `helios-app validate`
# checks both: a limit on an argument the tool does not take never matches, and a
# fact nobody supplies denies every call the rule covers.
#
# rules:
#   # 1. Require the arguments that identify the action. A payment with no payee
#   #    cannot be checked against anything.
#   - id: app.requires_identity
#     description: Every gated call names what it acts on
#     task_types: [{task}]
#     tools: [{gated_tools}]
#     message: >
#       this call is missing an argument the policy needs; supply it rather than
#       retrying without it
#     required_arguments: [{subject_args}]
#     severity: critical
#
#   # 2. Cap a numeric argument against a TRUSTED fact, not against what the
#   #    agent says. This is the difference between "we cap payments at the
#   #    invoice amount" and "we cap them at the amount the agent claims" — and
#   #    the second one is not a control.
#   - id: app.amount_matches_record
#     description: The amount may not exceed the recorded value
#     task_types: [{task}]
#     tools: [{gated_tools}]
#     message: the amount exceeds what is recorded for this subject
#     limits:
#       - argument: amount_cents
#         max_from_fact: amount_cents
#         on_exceed: hold
#     severity: critical
#
#   # 3. Hold above an absolute ceiling. `max` is a constant; use this where the
#   #    limit is a policy number rather than a property of the record.
#   - id: app.large_amount_approval
#     description: Owner approval above a fixed ceiling
#     task_types: [{task}]
#     tools: [{gated_tools}]
#     message: this amount needs owner approval before it proceeds
#     limits:
#       - argument: amount_cents
#         max: "1000000"          # integer cents; money is never a float
#         on_exceed: hold
#
#   # 4. Refuse outright when a trusted fact says so. Deny, not hold: there is no
#   #    amount the owner could approve that makes this safe.
#   - id: app.refuse_when_flag_set
#     description: Never act on a record flagged by the system of record
#     task_types: [{task}]
#     tools: [{gated_tools}]
#     message: >
#       this record is flagged; the action is refused until the owner clears the
#       flag out of band
#     deny_if_fact_true: [flag_set]
#     severity: critical
#
#   # 5. Ask the owner when a fact makes the action merely unusual rather than
#   #    wrong. This is a hold, so the agent stops and the owner decides.
#   - id: app.unusual_case_approval
#     description: Unusual cases go to the owner
#     task_types: [{task}]
#     tools: [{gated_tools}]
#     message: this case is unusual enough to need a human decision
#     approval_if_fact_true: [unusual]
#
#   # A frequency cap, if this app contacts anyone. `max_contacts_in_days` needs
#   # facts.py to implement contact_count_for, and the runtime to call
#   # record_contact after an execution that actually happened.
#   - id: app.contact_frequency
#     description: At most two contacts per subject in any 7-day window
#     task_types: [{task}]
#     tools: [{gated_tools}]
#     message: >
#       this subject has already been contacted twice in the last 7 days; stop and
#       tell the owner rather than trying harder
#     max_contacts_in_days:
#       within_days: 7
#       max: 2
#     severity: warn
"""


def audit_manifest(spec: AppSpec) -> str:
    """The audit manifest: every spec tool declared, policy left as a TODO.

    `side_effect` is guessed from the tool name's verb and each guess carries a
    `# REVIEW` comment, because the flag decides whether a call is gated at all.
    A wrong guess in the unsafe direction is caught later by the validator once
    `tools.py` is filled in — the two files must agree — but the reviewer still has
    to look.
    """
    task = f"{spec.app_id.replace('-', '_')}.operate"
    gated = [t for t in spec.tools if guess_side_effect(t)]
    reads = [t for t in spec.tools if not guess_side_effect(t)]
    subject_args = _subject_arguments(spec)

    lines: list[str] = []
    lines.append("# The authoritative in-app gate for "
                 f"{spec.identity.name}.")
    lines.append("#")
    lines.append("# This file is POLICY. The action agent may ask for anything;")
    lines.append("# what it gets is decided here, by code, failing closed.")
    lines.append("# The tool declarations are generated. The rules are not — see")
    lines.append("# the TODO block at the bottom.")
    lines.append("")
    lines.append('manifest_version: "1"')
    lines.append(f"app_id: {spec.app_id}")
    lines.append(f"name: {spec.identity.name} Audit Agent")
    lines.append("description: >")
    for chunk in _wrap(spec.identity.description.strip() or
                       f"Governs the action agent for {spec.app_id}.", 74):
        lines.append(f"  {chunk}")
    lines.append("")
    lines.append("tools:")
    for tool in spec.tools:
        lines.extend(_tool_declaration(tool, subject_args))
    lines.append("")
    lines.append("tasks:")
    lines.append(f"  {task}:")
    lines.append(f"    description: The default task for {spec.identity.name}")
    lines.append(f"    allowed_tools: [{', '.join(spec.tools)}]")
    lines.append("    ttl_seconds: 300")
    lines.append("")
    lines.append("# No rules are generated. A rule with a made-up ceiling passes")
    lines.append("# validation, reads like a control, and caps the wrong thing —")
    lines.append("# so the shapes are below, commented, with the exact keys.")
    lines.append(_RULE_TODO_BLOCK.format(
        task=task,
        gated_tools=", ".join(gated) or "<none: no tool name implies a side effect>",
        subject_args=", ".join(subject_args) or "subject_id",
    ).rstrip())
    lines.append("")
    lines.append("settings:")
    lines.append("  default_ttl_seconds: 300")
    lines.append("  contact_window_days: 7")
    lines.append(f"  log_path: {spec.app_id}-audit-log.jsonl")
    lines.append("  # Advisory review is NON-BINDING: it never changes a verdict.")
    lines.append("  advisory: [none]")
    lines.append("  allow_unsigned_grants: false")
    lines.append("")

    text = "\n".join(lines)
    _sanity_check_audit(spec, text, gated, reads)
    return text


def _tool_declaration(tool: str, subject_args: list[str]) -> list[str]:
    side_effect = guess_side_effect(tool)
    marker = "  # REVIEW: side_effect guessed from the tool name"
    lines = [f"  - name: {tool}"]
    lines.append(f"    description: >")
    lines.append(f"      TODO: what {tool} does, and what it changes. A description")
    lines.append(f"      the agent can act on, not a restatement of its name.")
    lines.append(f"    side_effect: {'true' if side_effect else 'false'}{marker}")
    if side_effect:
        # A gated tool with no schema cannot have its arguments checked before the
        # call happens, and the audit loader refuses one outright. So every guessed
        # side effect gets a real schema, with the subject keys the fact resolver
        # needs and `additionalProperties: false` so an extra argument is refused
        # rather than ignored.
        lines.append("    input_schema:")
        lines.append("      type: object")
        lines.append("      additionalProperties: false")
        lines.append(f"      required: [{', '.join(subject_args)}]")
        lines.append("      properties:")
        for arg in subject_args:
            lines.append(f"        {arg}:")
            lines.append("          type: string")
            lines.append("          minLength: 1")
        lines.append("        # TODO: add this tool's real arguments here, and")
        lines.append("        # mirror them in tools.py. Anything not listed is")
        lines.append("        # refused, because additionalProperties is false.")
    return lines


def _subject_arguments(spec: AppSpec) -> list[str]:
    """Subject keys this app's entities actually carry.

    The fact resolver keys on SUBJECT_KEYS only, so a gated tool that takes none of
    them gives the auditor no way to find the record — every fact-bound rule on it
    fails closed. Picking the keys that appear as entity fields means the generated
    schema can resolve them, rather than inventing an argument no table has.
    """
    fields: set[str] = set()
    for entity in spec.local_entities:
        for field in entity.fields:
            fields.add(field.name)
    found = [k for k in SUBJECT_KEYS if k in fields]
    return found or ["subject_id"]


def _sanity_check_audit(spec: AppSpec, text: str, gated: list[str],
                        reads: list[str]) -> None:
    """Parse what was just generated, so a broken template fails here.

    Without this, a YAML slip in the template produces a file that looks generated
    and does not load, and the failure surfaces later as a confusing validator
    error against the app rather than as a bug in the generator.
    """
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScaffoldError(
            f"the generated audit manifest for {spec.app_id} is not valid YAML "
            f"({_one_line(exc)}); this is a bug in app_builder.scaffold, not in "
            "the spec") from exc
    if not isinstance(document, dict):
        raise ScaffoldError(
            f"the generated audit manifest for {spec.app_id} is not a mapping; "
            "this is a bug in app_builder.scaffold")
    declared = {t["name"] for t in document.get("tools", [])}
    if declared != set(spec.tools):
        raise ScaffoldError(
            f"the generated audit manifest for {spec.app_id} declares {sorted(declared)} "
            f"but the spec lists {sorted(spec.tools)}; this is a bug in "
            "app_builder.scaffold")


# ---------------------------------------------------------------------------
# agents/<id>-action.json
# ---------------------------------------------------------------------------

def action_manifest(spec: AppSpec) -> str:
    """The action manifest, with the five-point operating contract.

    `mandate.limits` is emitted EMPTY on purpose. The loader accepts only four
    keys — `max_extension_days`, `extension_owner_approval_days`,
    `min_days_between_contacts`, `invoice_owner_approval_amount` — and all four are
    receivables-shaped. Anything else is rejected, so a generated app for any other
    domain cannot express policy here at all. The authoritative policy is the audit
    manifest; guessing receivables limits into an unrelated app would be worse than
    an empty block.
    """
    gated = [t for t in spec.tools if guess_side_effect(t)]
    instructions = _instructions(spec, gated)
    document = {
        "name": spec.app_id,
        "version": "1",
        "description": _one_line(
            spec.identity.description.strip()
            or f"The action agent for {spec.identity.name}."),
        "instructions": instructions,
        "tools": list(spec.tools),
        "mandate": {"allowed_tools": gated, "limits": {}},
        "max_turns": 12,
    }
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    _sanity_check_action(spec, document, gated)
    return text


def _instructions(spec: AppSpec, gated: list[str]) -> str:
    """The five-point operating contract, written for this app.

    Points 3 and 4 are what make an agent legible under constraint. An agent that
    argues with its gate is a liability; one that reports it is a product. Point 3
    in particular names the failure a reviewer looks for: rewording a held request
    and retrying it is circumventing a control, not negotiating.
    """
    tool_list = ", ".join(spec.tools) or "(none declared)"
    gated_list = ", ".join(gated) or "(none)"
    return (
        f"You are the action agent for {spec.identity.name}.\n\n"
        f"1. Read before you act. Call the read-only tools ({tool_list}) to find "
        "out what is true, and never state an amount, a date, a counterparty or a "
        "status you did not read from a tool result. If you have not read it, you "
        "do not know it.\n\n"
        f"2. Obtain the authorization the policy requires before any side effect. "
        f"The gated tools are: {gated_list}. A side effect is a PROPOSAL, not an "
        "action; the audit agent decides. If a policy requires a human-confirmed "
        "credential (for a payment, an auth_code from a confirmed intent), capture "
        "and confirm the intent first — a call without it is refused, so do not "
        "attempt one.\n\n"
        "3. If a call is HELD, stop and surface the owner's question verbatim. The "
        "hold carries a plain-language question written by the auditor, not by "
        "you. Do not reword the request and retry it, do not split it into smaller "
        "calls, and do not route it through a different tool to get past the hold. "
        "That is circumventing a control, and it is the specific failure this "
        "platform is built to prevent.\n\n"
        "4. If a call is DENIED, read the reason. Either fix what is actually wrong "
        "and try once, or report that the work cannot be done and say why. Never "
        "work around a denial, and never claim a denied action succeeded.\n\n"
        "5. Report per item what happened: done, held awaiting the owner, or "
        "refused with the reason. An item you could not finish is a result, not a "
        "failure to report."
    )


def _sanity_check_action(spec: AppSpec, document: dict[str, Any],
                         gated: list[str]) -> None:
    allowed_keys = {"name", "version", "description", "instructions", "tools",
                    "mandate", "model", "max_turns"}
    unknown = sorted(set(document) - allowed_keys)
    if unknown:
        raise ScaffoldError(
            f"the generated action manifest for {spec.app_id} carries unknown "
            f"key(s) {unknown}, which the loader rejects even for a `_comment`; "
            "this is a bug in app_builder.scaffold")
    if set(gated) - set(document["tools"]):
        raise ScaffoldError(
            f"the generated action manifest for {spec.app_id} allows a tool it "
            "does not declare; this is a bug in app_builder.scaffold")


# ---------------------------------------------------------------------------
# facts.py
# ---------------------------------------------------------------------------

def facts_module(spec: AppSpec) -> str:
    """A `FactSource` over the generated store, returning None for unknown subjects.

    The two invariants are stated in the generated file because they are the ones a
    helpful edit is most likely to break:

    1. Unknown is None, never zero. A missing fact is the auditor not knowing
       something it needs, which fails closed and denies. Returning 0 for an
       unknown balance would read as "no exposure" and quietly turn a fail-closed
       control into a fail-open one.
    2. Nothing here may consult the request. Only a subject id arrives. If a method
       accepted arguments, every claim the agent makes would become a fact.
    """
    entities = spec.local_entities
    getters = []
    for entity in entities:
        getters.append(f'    "{entity.name}": store.get_{entity.name},')
    lookups = "\n".join(getters) if getters else "    # no local entities"

    return f'''"""Trusted facts for the audit agent — the most important module in this app.

This is where "facts are not claims" stops being a slogan. The audit agent resolves
every value a rule depends on through this object. It never reads the action
agent's arguments for those values: the agent gets to *name* which record it means,
and nothing more.

Two invariants this class must not break:

1. **Unknown is None, never zero.** A missing fact is the auditor not knowing
   something it needs, which fails closed and denies. Returning 0 for an unknown
   balance would read as "no exposure" and quietly turn a fail-closed control into
   a fail-open one.
2. **Nothing here may consult the request.** Only a subject id arrives. If a method
   accepted arguments, every claim the agent made would become a fact.

Generated by app-builder. The method bodies resolve against `store.py`; the fact
KEYS are yours to write, because only this app knows what its policy depends on.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import store

HERE = Path(__file__).resolve().parent


class {_class_name(spec.app_id)}FactSource:
    """Resolves trusted facts for {spec.app_id}'s audit rules."""

    #: Subject id -> the accessor that can resolve it. Generated from the spec's
    #: entities; extend it if a subject can be identified another way.
    _LOOKUPS: dict[str, Callable[[sqlite3.Connection, str], dict[str, Any] | None]] = {{
{lookups}
    }}

    def __init__(self, conn: sqlite3.Connection, *,
                 contact_window_days: int = 7) -> None:
        self._conn = conn
        self.contact_window_days = contact_window_days
        #: subject id -> contact dates, oldest first. Seeded lazily and extended
        #: only by `record_contact`, which the runtime calls after an execution
        #: that actually happened.
        self._contacts: dict[str, list[date]] = {{}}

    # -- contact history ----------------------------------------------------
    #
    # `record_contact` is REQUIRED, not optional. The gated runtime calls
    # `fact_source.record_contact(...)` after a successful contact, and it does so
    # outside its try/except — so a fact source without this method takes the whole
    # run down with an AttributeError.

    def record_contact(self, subject_id: str, when: date | None = None) -> None:
        """Note that a contact really happened.

        Called by the runtime after an approved execution — never before, and never
        on the agent's say-so. That asymmetry is the point: the agent cannot lower
        its own contact count by describing the subject as untouched, and cannot
        raise it either.

        Deduplicated by date, so a resumed run cannot inflate the count and thereby
        deny a follow-up that is legitimate.
        """
        when = when or date.today()
        history = self._contacts.setdefault(subject_id, [])
        if when not in history:
            history.append(when)
            history.sort()

    # -- FactSource protocol -------------------------------------------------

    def amount_for(self, subject_id: str) -> Decimal | None:
        """The recorded amount for a subject, or None if there is no such subject.

        TODO: point this at the field that holds the authoritative amount. Until
        then it returns None, which makes every amount-bound rule deny — safe, and
        loud, rather than silently permitting.
        """
        record = self._resolve(subject_id)
        if record is None:
            return None
        # TODO: replace `amount` with this app's authoritative amount field.
        raw = record.get("amount")
        if raw is None:
            return None
        return Decimal(str(raw))

    def contact_count_for(self, subject_id: str, within_days: int) -> int | None:
        """Contacts with this subject inside a trailing window.

        Returns None for a subject this app has never heard of, so a frequency rule
        denies rather than treating "never contacted" as a licence to contact.
        """
        if self._resolve(subject_id) is None:
            return None
        cutoff = date.today() - timedelta(days=int(within_days))
        history = self._contacts.setdefault(subject_id, [])
        return sum(1 for when in history if when >= cutoff)

    def counterparty_for(self, subject_id: str) -> str | None:
        """The counterparty's name, for owner-facing messages and the audit log."""
        record = self._resolve(subject_id)
        if record is None:
            return None
        # TODO: replace `name` with this app's counterparty field.
        return str(record.get("name") or subject_id)

    def payment_details_changed_for(self, subject_id: str) -> bool | None:
        """Whether the counterparty's payment details recently changed.

        None means "this app has no such fact", which is honest. Returning False
        would assert a fact nobody recorded, and a rule reading it would then pass
        on a fraud case.
        """
        # TODO: implement if this app pays anyone. Leave as None otherwise.
        return None

    def extra_facts_for(self, subject_id: str) -> dict[str, Any]:
        """App-specific signals, keyed by the names the audit manifest's rules use.

        These keys are the contract with agents/{spec.app_id}-audit.yaml. A rule
        naming a key that is not returned here fails closed and denies everything
        the rule covers — safe, but it looks like a broken app at demo time, which
        is why `helios-app validate` checks the two against each other.

        TODO: return every key the rules in the audit manifest name.
        """
        facts: dict[str, Any] = {{}}
        record = self._resolve(subject_id)
        if record is None:
            return facts
        # Example of the shape a rule expects. Replace with this app's own facts:
        #
        #   amount = record.get("amount")
        #   if amount is not None:
        #       facts["amount_cents"] = int(Decimal(str(amount)) * 100)
        #   facts["flag_set"] = bool(record.get("flagged"))
        return facts

    # -- internal ------------------------------------------------------------

    def _resolve(self, subject_id: str) -> dict[str, Any] | None:
        """Find the record a subject id refers to, or None.

        Tries each entity in turn. Only one should match for a given id shape; if
        two do, the first in spec order wins, which is deterministic and therefore
        debuggable.
        """
        for lookup in self._LOOKUPS.values():
            record = lookup(self._conn, subject_id)
            if record is not None:
                return record
        return None


def build_fact_source(conn: sqlite3.Connection | None = None,
                      *, contact_window_days: int = 7) -> {_class_name(spec.app_id)}FactSource:
    """Convenience constructor: opens the store's database if none is given."""
    if conn is None:
        conn = store.connect()
        store.init_db(conn)
    return {_class_name(spec.app_id)}FactSource(conn, contact_window_days=contact_window_days)


__all__ = ["{_class_name(spec.app_id)}FactSource", "build_fact_source"]
'''


# ---------------------------------------------------------------------------
# tools.py
# ---------------------------------------------------------------------------

def tools_module(spec: AppSpec) -> str:
    """One `ToolDefinition` per spec tool, with stubs that refuse rather than pretend.

    A stub that returns a plausible empty result would let a demo "succeed" while
    doing nothing. Raising `NotImplementedError` with a message naming the tool, the
    arguments it received and the file to edit makes the next step obvious, and
    makes an unfilled tool impossible to mistake for a working one.

    `output_schema` is emitted for every tool because `ToolDefinition` requires a
    non-empty one and refuses to construct without it.
    """
    parts: list[str] = []
    parts.append(f'''"""The tools {spec.identity.name}'s agents may actually do.

Generated by app-builder from helios-app.yaml. One ToolDefinition per tool the spec
declares, with `side_effect` matching agents/{spec.app_id}-audit.yaml — the two must
agree, and `helios-app validate` fails if they do not.

Every handler raises NotImplementedError on purpose. A stub that returned a
plausible empty result would let a demo "succeed" while doing nothing at all; a
stub that raises names the tool, the arguments it was given and the function to
implement, so an unfilled tool cannot be mistaken for a working one.
"""
from __future__ import annotations

from typing import Any, Mapping

from agent_toolkit.contracts import ToolDefinition

#: Every tool name this app exposes. Both manifests must agree with it, and
#: tests/test_controls.py asserts that they do.
TOOL_NAMES: tuple[str, ...] = (
{chr(10).join(f'    "{t}",' for t in spec.tools)}
)


def _not_implemented(name: str, args: Mapping[str, Any]) -> Any:
    """Refuse loudly, naming what to write instead."""
    raise NotImplementedError(
        f"{{name}} is not implemented. It was called with {{dict(args)!r}}. "
        f"Implement the handler for {{name}} in tools.py: return a dict matching "
        "its output_schema, and make every side effect go through the store so the "
        "fact source can see it. Until then this tool must not be callable, because "
        "a stub that returns an empty result would let a demo succeed while doing "
        "nothing."
    )
''')

    for tool in spec.tools:
        parts.append(_tool_definition(spec, tool))

    parts.append('''

def build_tools(*, conn: Any = None, bundle: Any = None) -> list[ToolDefinition]:
    """Every tool, wired to this app's data layer.

    `bundle` is the platform's financial services (`fin_tools.build_world()`), for
    tools that move money or publish to the shared graph. `conn` is the store's
    SQLite connection, for tools that read this app's own records.
    """
    return [
''' + "".join(f"        make_{_safe_fn_name(tool)}(),\n" for tool in spec.tools)
               + '''    ]


__all__ = ["TOOL_NAMES", "build_tools"]
''')
    return "".join(parts)


def _tool_definition(spec: AppSpec, tool: str) -> str:
    fn = _safe_fn_name(tool)
    side_effect = guess_side_effect(tool)
    subject_args = _subject_arguments(spec)
    if side_effect:
        properties = "".join(
            f'            "{arg}": {{"type": "string", "minLength": 1}},\n'
            for arg in subject_args)
        required = ", ".join(f'"{a}"' for a in subject_args)
        input_schema = (
            "{\n"
            '    "type": "object",\n'
            "    \"additionalProperties\": False,\n"
            f'    "required": [{required}],\n'
            '    "properties": {\n'
            f"{properties}"
            "        # TODO: add this tool's real arguments. Anything not listed\n"
            "        # is refused, because additionalProperties is False, and this\n"
            "        # schema must mirror the one in the audit manifest.\n"
            "    },\n"
            "}")
    else:
        input_schema = (
            "{\n"
            '    "type": "object",\n'
            '    "additionalProperties": False,\n'
            '    "properties": {},\n'
            "}")
    gating = "gated: it becomes a proposal the audit agent decides" if side_effect \
        else "read-only, so it runs without a grant"

    return f'''

def make_{fn}() -> ToolDefinition:
    """{tool} — {gating}."""

    def handler(args: Mapping[str, Any]) -> Any:
        # TODO: implement. Read from the store, and for a side effect write
        # through it so the fact source sees the change rather than the agent's
        # description of it.
        return _not_implemented("{tool}", args)

    return ToolDefinition(
        name="{tool}",
        description=(
            "TODO: what {tool} does and what it changes. Write this for the model "
            "that will call it: name the arguments, say what comes back, and say "
            "what it must NOT be used for."
        ),
        input_schema={input_schema},
        output_schema={{
            "type": "object",
            "properties": {{"ok": {{"type": "boolean"}}}},
            "required": ["ok"],
            "additionalProperties": False,
        }},
        side_effect={side_effect},
        handler=handler,
    )
'''


def _safe_fn_name(tool: str) -> str:
    """A Python identifier for a tool name (`payments.send` -> `payments_send`)."""
    return re.sub(r"[^a-z0-9]+", "_", tool.lower()).strip("_") or "tool"


def _class_name(app_id: str) -> str:
    """A class-name prefix for an app id (`invoice-payer` -> `InvoicePayer`)."""
    return "".join(part.capitalize() for part in re.split(r"[^a-z0-9]+", app_id.lower())
                   if part)


# ---------------------------------------------------------------------------
# graph.py
# ---------------------------------------------------------------------------

def graph_module(spec: AppSpec, result: ScaffoldResult) -> str:
    """Publish and subscribe call sites from the spec's `graph` declarations.

    The shared graph's transport is not settled yet, so these are call sites
    against `fin_tools`' graph object rather than a client library. When the
    service interface lands, connecting an existing app is filling in a transport
    rather than restructuring it.

    Note the real API: `upsert_verified(kind, key, attrs, *, source)` and
    `add_observation(kind, key, attrs, *, observer)`. There is no
    `publish_verified`; `add_observation` refuses to overwrite a verified node, so
    an agent's claim can never displace a fact.
    """
    local_names = {e.name for e in spec.local_entities}

    publishes: list[tuple[str, str]] = []
    for link in spec.graph.publishes:
        for event in (link.events or ["changed"]):
            publishes.append((link.entity, event))
        if local_names and link.entity not in local_names:
            result.warnings.append(
                f"graph.publishes entity {link.entity!r} is not declared in "
                f"data.local, so publish_{_safe_fn_name(link.entity)}_* writes "
                "state this app owns no record of. Check that it is genuinely "
                "cross-app state and not a typo.")

    reads = [(link.entity, event) for link in spec.graph.reads
             for event in (link.events or ["changed"])]
    for entity, event in reads:
        result.warnings.append(
            f"graph.reads declares {entity}.{event} but nothing consumes it yet; "
            f"subscribe_{_safe_fn_name(entity)} is a stub, so wire it to the code "
            "that should react or remove the declaration.")

    parts = [f'''"""Knowledge-graph call sites for {spec.identity.name}.

Generated from the spec's `graph` section. The shared graph's transport is not
settled yet, so these are call sites against the platform's graph object rather
than a client library: when the service interface lands, connecting this app is
filling in a transport, not restructuring it.

Provenance matters here. `upsert_verified` writes a record sourced from this app's
own ledger, and `add_observation` writes an agent's account of something it was
told. The graph REFUSES to let an observation overwrite a verified node, so a claim
can never displace a fact. Publish verified data only where this app is the system
of record for it.
"""
from __future__ import annotations

from typing import Any, Callable

#: What this app declares itself as in the graph's provenance field.
SOURCE = "{spec.app_id}"

#: Reads declared by the spec. Each needs a consumer; the subscribe stubs below
#: are where one attaches.
READS: tuple[tuple[str, str], ...] = (
{chr(10).join(f'    ("{e}", "{v}"),' for e, v in reads) or "    # none declared"}
)


def _require_graph(graph: Any) -> Any:
    if graph is None:
        raise ValueError(
            "no knowledge graph was supplied. Pass the bundle's graph "
            "(`fin_tools.build_world().graph`) — publishing nowhere would silently "
            "drop cross-app state, and another app waiting on it would never know."
        )
    return graph
''']

    for entity, event in publishes:
        fn = _safe_fn_name(f"{entity}_{event}")
        parts.append(f'''

def publish_{fn}(graph: Any, key: str, attrs: dict[str, Any]) -> dict[str, Any]:
    """Publish {entity}.{event} as a VERIFIED record sourced from this app.

    Call this only where {spec.app_id} is the system of record for {entity}. An
    agent's account of a conversation is an observation, not a fact: use
    `graph.add_observation(..., observer=SOURCE)` for that instead.
    """
    _require_graph(graph)
    if not key:
        raise ValueError(
            "publish_{fn} needs the {entity} key; an empty key would write a node "
            "nothing can look up again")
    return graph.upsert_verified("{entity}", str(key), dict(attrs), source=SOURCE)
''')

    for entity in sorted({e for e, _ in reads}):
        fn = _safe_fn_name(entity)
        parts.append(f'''

def subscribe_{fn}(graph: Any, handler: Callable[[dict[str, Any]], None]) -> None:
    """Attach a consumer for {entity} changes read from the graph.

    TODO: this is a stub. The graph's subscription transport is not settled, so
    until it is, poll `graph.nodes` for kind == "{entity}" or wire this to
    whatever event source the host provides. Until it is wired, nothing in this
    app reacts to {entity} changes originating elsewhere — which is why
    `helios-app validate` warns about a declared read with no consumer.
    """
    _require_graph(graph)
    raise NotImplementedError(
        "subscribe_{fn} is not wired: the shared graph's subscription transport is "
        "not settled yet. Either poll graph.nodes for kind == '{entity}', or remove "
        "`graph.reads` for {entity} from helios-app.yaml if this app does not "
        "actually need to react to it."
    )
''')

    parts.append('''

__all__ = ["SOURCE", "READS"]
''')
    return "".join(parts)


# ---------------------------------------------------------------------------
# app.py
# ---------------------------------------------------------------------------

# The scenario runner, the driver it replays, and the two page renderers.
#
# This is a PLAIN string, not part of app_module's f-string, and that is
# deliberate. Everything here needs no value from the spec, and keeping it out of
# the f-string means its braces are literal — no `{{` doubling. Doubling is the
# single easiest way to silently corrupt generated code: a brace that should have
# been doubled is interpolated by the GENERATOR instead of the generated module, so
# the bug surfaces as a NameError in scaffold.py while the real fault is in text
# that was never checked. Two such bugs were found and fixed by adding test_boot.py.
#
# Semantics mirror apps/helios-collect/app.py, including the precedence in
# _summarize. That function is the part worth getting right: a run whose only side
# effect was REFUSED still reports COMPLETED, because a denial is fed back to the
# model as an observation and the loop then finishes normally. Reporting the loop's
# status would call a blocked payment a success.
_APP_RUNNER = '''
class ScriptedDriver:
    """A driver that replays a scenario's calls, then reports.

    It ignores history and replays its script, which is what makes the offline demo
    trustworthy: it proves the GATE works independently of what any model does.
    Substituting a real Qwen loop means replacing this object with one that reads
    `tools` and `history` and returns calls — the gate underneath does not change,
    and that separability is the architecture-defence point.

    It does not retry a held or denied call, per the action manifest's operating
    contract. Retrying a reworded variant is circumventing a control, and it is the
    specific failure mode a reviewer looks for.
    """

    def __init__(self, calls, final_text=""):
        self._calls = [(name, dict(args)) for name, args in calls]
        self._index = 0
        self.final_text = final_text or "Work on this subject is complete."

    def next_calls(self, *, task, instructions, tools, history):
        from agent_integration.runtime import ToolCall

        if self._index >= len(self._calls):
            return self.final_text
        name, args = self._calls[self._index]
        self._index += 1
        return [ToolCall(name=name, arguments=args)]


def run_scenario(app, scenario, owner, *, run_id=None):
    """Run one scenario through the platform's gated loop, resolving holds."""
    from agent_integration.runtime import COMPLETED

    task = f"{scenario.task_type} for {scenario.subject_id}"
    outcome = Outcome(
        subject_id=scenario.subject_id, task_type=scenario.task_type,
        control=scenario.control, expects=scenario.expects,
    )

    # Request every tool the app has. The auditor grants only what this task's
    # allowlist permits, and the loop offers the agent nothing else — an ungranted
    # tool is invisible rather than rejected after the fact.
    result = app.runtime.run(
        driver=ScriptedDriver(scenario.calls),
        task=task, task_type=scenario.task_type, run_id=run_id,
        requested_tools=list(app_tools.TOOL_NAMES),
    )

    outcome.run_id = result.run_id
    outcome.status = result.status
    outcome.detail = result.detail
    outcome.actions = list(result.actions)
    outcome.holds = list(result.holds)

    final_status = result.status
    # A hold stops the loop. Resolve it with the owner, then resume — which executes
    # exactly the action the owner was shown and nothing else.
    for hold in list(result.holds):
        approve = owner.decide_hold(hold)
        outcome.owner_decisions.append({"hold_id": hold.get("hold_id"),
                                        "approved": approve})
        resumed = app.runtime.resume(
            driver=ScriptedDriver([], final_text="Resumed after the owner's decision."),
            run_id=result.run_id, hold_id=str(hold.get("hold_id")),
            decision=approve, decided_by="owner",
            task=task, task_type=scenario.task_type,
        )
        outcome.actions.extend(resumed.actions)
        final_status = resumed.status
        # The pre-hold detail describes a pause that is now over. Keeping it would
        # report a completed run as "paused for an owner decision".
        outcome.detail = resumed.detail if resumed.status != COMPLETED else ""
        outcome.holds = [h for h in outcome.holds
                         if h.get("hold_id") != hold.get("hold_id")]

    # final_status is the status of the LAST run, which after a resume is the
    # resumed one — passing the pre-resume status here would report an
    # approved-and-executed action as still held.
    outcome.status = _summarize(outcome, fallback=final_status)
    outcome.reason = _reason_for(outcome)
    return outcome


def _reason_for(outcome):
    """The human-readable reason behind an outcome, from what the gate recorded."""
    for action in outcome.actions:
        verdict = action.get("verdict") or {}
        reason = str(verdict.get("reason") or "").strip()
        if reason:
            return reason
    return (outcome.detail or "").strip()


def _summarize(outcome, *, fallback):
    """One word for what happened, derived from the actions rather than the loop.

    Precedence runs from the most terminal outcome backwards:

      1. an error beats everything — something broke;
      2. a reject beats an execution — if any action was refused, the scenario did
         not complete cleanly, whether a rule refused it or the owner declined it;
      3. an execution means the work landed;
      4. a still-pending hold means the scenario is paused;
      5. otherwise nothing had any effect at all.

    Note what this deliberately does NOT do: it never reports `done` merely because
    the loop finished. A run whose only side effect was refused finishes normally,
    and calling that a success would hide the control firing.
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


def render_records(app):
    """The records in the store, as the summary page would show them."""
    lines = []
    for entity in store.ENTITIES:
        rows = getattr(store, f"list_{entity}")(app.conn)
        lines.append(f"{entity}: {len(rows)} row(s)")
        for row in rows[:20]:
            lines.append(f"  {row}")
    return "\\n".join(lines)


def render_dashboard(app, outcomes):
    """The `summary` pattern as text: outcome counts, then the records."""
    counts = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    lines = [f"{app.app_id} — {len(outcomes)} scenario(s)"]
    for status in sorted(counts):
        lines.append(f"  {status}: {counts[status]}")
    lines.append("")
    lines.append(render_records(app))
    return "\\n".join(lines)
'''


def app_module(spec: AppSpec) -> str:
    """Composition of `build_integration`, matching `apps/helios-collect/app.py`.

    The order of operations is load-bearing and is copied from the reference app:
    build the integration, THEN swap in this app's fact source. The platform's
    `HeliosStoreFactSource` cannot supply app-specific facts, and a fact-bound rule
    whose fact never resolves denies everything — so the swap is not optional for an
    app with real policy.
    """
    return f'''"""{spec.identity.name} — assembled on the Helios platform.

    python3 app.py --list               # what each scenario demonstrates
    python3 app.py                      # narrated run over every scenario
    python3 app.py --scenario <id>      # one control path (repeatable)
    python3 app.py --json               # machine-readable results
    python3 app.py --trace              # per-step actions and verdicts
    python3 app.py --dashboard          # the records, as the summary page shows them
    python3 app.py --run-dir DIR        # where to write run logs

Generated by app-builder. The composition below is the part that is already right:
it builds the platform's gated loop over THIS app's fact source and manifests, so
every side effect goes through the audit gate. What is not written yet is the
policy (agents/{spec.app_id}-audit.yaml), the facts (facts.py), the tool handlers
(tools.py) and the scenarios at the bottom of this file.

Nothing here sets a dummy signing key. With no HELIOS_AUDIT_SIGNING_KEY in the
environment, `build_integration` generates an ephemeral one so grants are still
signed — the honest demo, and one that cannot be shipped by accident.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent_integration.wiring import build_integration
from agent_toolkit.manifest import load_manifest as load_action_manifest
from audit_agent.facts import FactResolver
from audit_agent.manifest import AuditManifest
from fin_tools import build_world

import store
import tools as app_tools
from facts import {_class_name(spec.app_id)}FactSource

HERE = Path(__file__).resolve().parent

AUDIT_MANIFEST = HERE / "agents" / "{spec.app_id}-audit.yaml"
ACTION_MANIFEST = HERE / "agents" / "{spec.app_id}-action.json"

#: The default task declared in the audit manifest.
TASK_OPERATE = "{spec.app_id.replace('-', '_')}.operate"


@dataclass
class App:
    """A running {spec.app_id}: its store, its auditor, its loop."""

    conn: Any
    fact_source: {_class_name(spec.app_id)}FactSource
    integration: Any
    runtime: Any
    action_manifest: Any
    audit_manifest: Any
    bundle: Any
    audit_log_path: Path

    @property
    def app_id(self) -> str:
        return self.audit_manifest.app_id

    def tools_for(self, task_type: str) -> list[str]:
        return list(self.audit_manifest.tasks[task_type].allowed_tools)

    def close(self) -> None:
        self.integration.close()


def build_app(run_dir: Path | str | None = None, *,
              data_dir: Path | str | None = None) -> App:
    """Assemble the app from its manifests, data and the platform packages."""
    run_dir = Path(run_dir) if run_dir is not None else (HERE / "runs")
    run_dir.mkdir(parents=True, exist_ok=True)
    data_root = Path(data_dir) if data_dir is not None else (run_dir / "audit")
    data_root.mkdir(parents=True, exist_ok=True)

    conn = store.connect()
    store.init_db(conn)

    # The platform's financial services: the ledger money moves through and the
    # shared graph other Helios apps read.
    bundle = build_world()

    action_manifest = load_action_manifest(ACTION_MANIFEST)
    audit_manifest = AuditManifest.load(AUDIT_MANIFEST)

    # The single most important cross-file invariant, enforced here as well as by
    # `helios-app validate`: every tool the action agent may offer must be declared
    # by the audit agent. An undeclared tool is not denied — it is never looked at,
    # so it runs with no gate at all.
    declared = {{t.name for t in audit_manifest.tools}}
    offered = set(action_manifest.tools) | set(action_manifest.mandate.allowed_tools)
    undeclared = sorted(offered - declared)
    if undeclared:
        raise ValueError(
            f"action manifest offers tool(s) the audit manifest has not declared: "
            f"{{undeclared}}. An ungated side effect is not allowed — declare them "
            f"in {{audit_manifest.app_id}}'s audit manifest.")

    # And the app must actually provide every tool both manifests promise. A
    # declared-but-absent tool is denied at runtime, which fails closed but reads
    # as a broken app.
    provided = set(app_tools.TOOL_NAMES)
    missing = sorted(declared - provided)
    if missing:
        raise ValueError(
            f"the audit manifest declares tool(s) this app does not implement: "
            f"{{missing}}. Implement them in tools.py, or remove them from the "
            "manifest.")

    audit_log_path = data_root / "{spec.app_id}-audit-log.jsonl"

    tools = app_tools.build_tools(conn=conn, bundle=bundle)

    # The platform's gated loop. `manifest_path` points at THIS app's audit
    # manifest, so authority comes from a reviewed file rather than from anything
    # the app generates for itself at runtime.
    integration = build_integration(
        tools=tools,
        app_id=audit_manifest.app_id,
        agent_id=action_manifest.name,
        agent_name=action_manifest.name,
        instructions=action_manifest.instructions,
        manifest_path=AUDIT_MANIFEST,
        runs_dir=run_dir,
        data_dir=data_root,
        audit_log_path=audit_log_path,
    )

    # Swap in this app's fact source. The platform's default cannot supply
    # app-specific facts, and a fact-bound rule whose fact never resolves denies
    # everything. Replacing the resolver rather than editing the platform keeps
    # this app out of other packages' code; `AuditAgent.facts` is an injected
    # attribute for exactly this reason.
    fact_source = {_class_name(spec.app_id)}FactSource(conn)
    integration.audit_agent.facts = FactResolver(fact_source)
    integration.runtime.fact_source = fact_source
    integration.fact_source = fact_source
    integration.runtime.max_turns = action_manifest.max_turns

    return App(
        conn=conn, fact_source=fact_source, integration=integration,
        runtime=integration.runtime, action_manifest=action_manifest,
        audit_manifest=audit_manifest, bundle=bundle,
        audit_log_path=audit_log_path,
    )


# ---------------------------------------------------------------------------
# The owner — a human checkpoint, not part of the agent
# ---------------------------------------------------------------------------

class ScriptedOwner:
    """A stand-in owner so the app runs offline and deterministically.

    Deliberately permissive: it approves everything it is shown. That is the point
    rather than an oversight. A scenario that is DENIED never reaches this object at
    all, so a busy owner rubber-stamping their way through the queue still cannot
    make it happen. The control does not depend on the human noticing, and it does
    not depend on the agent behaving.
    """

    def __init__(self, *, verbose: bool = True) -> None:
        self.verbose = verbose
        self.decisions: list[dict[str, Any]] = []
        self.asked: list[str] = []

    def _say(self, text: str) -> None:
        if self.verbose:
            print(text)

    def decide_hold(self, hold: Mapping[str, Any]) -> bool:
        """Approve or decline one held action.

        The owner sees the auditor's own question, never a summary the agent wrote
        about itself.
        """
        question = str(hold.get("owner_question") or hold.get("reason") or "")
        self.asked.append(str(hold.get("hold_id")))
        approve = True
        self._say(f"    [owner asked] {{question.strip()}}")
        self._say(f"    [owner decides] {{'approve' if approve else 'decline'}}")
        self.decisions.append({{
            "hold_id": hold.get("hold_id"), "tool": hold.get("tool"),
            "approved": approve,
        }})
        return approve


# ---------------------------------------------------------------------------
# The scenarios: one control path each
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Scenario:
    """One subject, one task, one control path.

    `calls` is what the agent asks for. It is a fixed script rather than a model's
    output on purpose: the thing being demonstrated is the gate, not the reasoning,
    and a script can include requests that SHOULD be refused without hoping a model
    decides to misbehave.
    """

    subject_id: str
    task_type: str
    calls: tuple[tuple[str, dict[str, Any]], ...]
    control: str
    expects: str
    note: str = ""


def scenarios() -> list[Scenario]:
    """TODO: the demo script. One entry per control.

    Write one scenario per control this app has, and one for each of these six
    outcomes — they are the evidence that the gate works rather than that the
    agent is well behaved:

      * allowed        — a normal call, inside every limit
      * held, approved — over a ceiling; the owner says yes; it runs with the
                         EXACT arguments that were held
      * held, declined — the owner says no; nothing happens
      * denied         — refused outright, with the reason, and never reaching
                         the owner at all
      * lying agent    — the agent claims a value the fact source contradicts,
                         and the claim changes nothing
      * undeclared     — a tool the audit manifest never declared is refused
    """
    return []


@dataclass
class Outcome:
    """What happened to one scenario, in the shape the UI pages consume."""

    subject_id: str
    task_type: str
    control: str
    expects: str
    reason: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    run_id: str = ""
    status: str = ""
    detail: str = ""
    actions: list[dict[str, Any]] = field(default_factory=list)
    holds: list[dict[str, Any]] = field(default_factory=list)
    owner_decisions: list[dict[str, Any]] = field(default_factory=list)
''' + _APP_RUNNER + f'''

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="{spec.identity.name} on the Helios platform")
    parser.add_argument("--scenario", action="append", default=None,
                        help="run one subject id (repeatable); default: all")
    parser.add_argument("--list", action="store_true",
                        help="list the scenarios and what each demonstrates")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable results")
    parser.add_argument("--trace", action="store_true",
                        help="print every action and verdict")
    parser.add_argument("--dashboard", action="store_true",
                        help="print the records and stop")
    parser.add_argument("--run-dir", default=None, help="where to write run logs")
    args = parser.parse_args(argv)

    if args.list:
        listed = scenarios()
        if not listed:
            print("no scenarios yet — write them in scenarios(), one per control")
            return 0
        for scenario in listed:
            print(f"  {{scenario.subject_id:<12}} {{scenario.control}}")
            print(f"      expects: {{scenario.expects}}")
            if scenario.note:
                print(f"      note:    {{scenario.note}}")
        return 0

    app = build_app(run_dir=args.run_dir)
    try:
        selected = scenarios()
        if args.scenario:
            wanted = set(args.scenario)
            selected = [s for s in selected if s.subject_id in wanted]

        if args.dashboard and not selected:
            # No scenarios written yet, so there is nothing to summarise. Showing
            # the records is still useful — it proves the store and its fixtures
            # load, which is the other half of "does this app work".
            print(render_records(app))
            return 0

        if not selected:
            print("no scenarios to run — write them in scenarios(), one per control")
            print()
            print(render_records(app))
            return 0

        owner = ScriptedOwner(verbose=not args.json)
        outcomes = [run_scenario(app, scenario, owner) for scenario in selected]

        if args.json:
            import json as _json

            print(_json.dumps([
                {{k: v for k, v in vars(o).items() if k != "steps"}} |
                {{"steps": len(o.steps)}}
                for o in outcomes
            ], indent=2, default=str))
            return 0

        if args.dashboard:
            print(render_dashboard(app, outcomes))
            return 0

        for outcome in outcomes:
            marker = {{"done": "OK  ", "held": "WAIT", "refused": "DENY",
                      "declined_by_owner": "DENY", "error": "ERR ",
                      "read_only": "READ", "no_effect": "    ",
                      "held_unresolved": "WAIT"
                      }}.get(outcome.status, "??? ")
            print(f"{{marker}} {{outcome.subject_id}}: {{outcome.status}}"
                  f" — {{outcome.control}}")
            if outcome.reason:
                print(f"        {{outcome.reason}}")
            if args.trace:
                for step in outcome.steps:
                    print(f"        {{step}}")

        # Exit non-zero when a scenario did not reach the outcome its author
        # declared, so `python app.py` can gate CI. A scenario that expects any
        # outcome opts out with expects="any".
        failures = [o for o in outcomes
                    if o.expects != "any" and o.status != o.expects]
        if failures:
            print()
            print(f"{{len(failures)}} of {{len(outcomes)}} scenario(s) did not "
                  f"reach the expected outcome:")
            for outcome in failures:
                print(f"  {{outcome.subject_id}}: got {{outcome.status}}, "
                      f"expected {{outcome.expects}}")
            return 1
        return 0
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
'''


# ---------------------------------------------------------------------------
# server.py
# ---------------------------------------------------------------------------

def server_module(spec: AppSpec) -> str:
    """The ASGI entry the deployment engine runs, with flat routes.

    Routes are FLAT (`/`, `/holds`, `/runs`), not nested, and every URL the
    templates emit is relative. The deployment host strips `/apps/<slug>` before
    the request reaches the app, so an absolute `/static/...` resolves to the host
    root and 404s — and a nested route would resolve `static/...` against the wrong
    prefix.

    `build_app` is imported lazily inside the request handlers. Building the app
    opens a database and loads both manifests, so doing it at import time would make
    `helios deploy`'s smoke test depend on data being present, and would turn a
    missing fixture into an unstartable container.
    """
    pages = ", ".join(f'"{p.id}"' for p in spec.pages)
    return f'''"""ASGI entry point for {spec.identity.name}.

    uvicorn server:app --port 3000

`helios.yaml` names this module as the deployment entry (`entry: server:app`).

Routes are FLAT and every URL the templates emit is RELATIVE. The deployment host
strips /apps/<slug> before the request reaches the app, so an absolute
"/static/tokens.css" would resolve to the host root and 404, and a nested route
would resolve "static/..." against the wrong prefix.

`build_app` is imported inside the handlers rather than at module scope. Building
the app opens a database and loads both manifests, so importing it eagerly would
make the engine's smoke test depend on data being present and would turn a missing
fixture into a container that will not start.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app_builder.kit import build_env

HERE = Path(__file__).resolve().parent

#: Generated from the spec's ui.pages: {pages}
PAGES: tuple[str, ...] = ({pages},)

app = FastAPI(title={json.dumps(spec.identity.name)}, version="0.1.0")

app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

_templates = build_env(HERE / "templates")


def _parse_qs(body: str) -> dict[str, list[str]]:
    """Parse a urlencoded form body with the stdlib.

    Deliberately not `await request.form()`: Starlette asserts that
    `python-multipart` is installed before parsing ANY form, including
    urlencoded ones, and that package is not a Helios dependency. Depending on it
    here would make every generated app fail its first approval POST with an
    AssertionError that names a package the app never asked for.
    """
    from urllib.parse import parse_qs

    return parse_qs(body, keep_blank_values=True)


def _app() -> Any:
    """Build the app on first use, then reuse it."""
    global _built
    if _built is None:
        from app import build_app
        _built = build_app()
    return _built


_built: Any = None


@app.get("/health")
def health() -> JSONResponse:
    """Liveness. The engine polls this after deploy; it must not need the database.

    Deliberately shallow: a health check that fails because a fixture is missing
    takes the whole app offline, which is a worse outcome than reporting that the
    process is up and the data is not.
    """
    return JSONResponse({{"status": "ok", "app_id": "{spec.app_id}"}})


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    """The summary page."""
    return _templates.get_template("summary.html.j2").render(
        app_name={json.dumps(spec.identity.name)},
        heading={json.dumps(spec.identity.name)},
        pages=_nav("/"),
        tiles=[],
        holds_count=0,
    )


@app.get("/holds", response_class=HTMLResponse)
def holds() -> str:
    """The approval queue: every action the audit agent held."""
    built = _app()
    pending = built.integration.audit_agent.pending_holds()
    return _templates.get_template("approval_queue.html.j2").render(
        app_name={json.dumps(spec.identity.name)},
        heading="Needs your approval",
        pages=_nav("/holds"),
        holds=[h.model_dump(mode="json") for h in pending],
    )


@app.post("/holds/decide", response_class=HTMLResponse)
async def decide(request: Request) -> str:
    """Approve or decline one held action.

    The decision goes through the audit bridge, not through app state, because
    whether an owner approved is the auditor's fact to own — on the file and HTTP
    transports the decision may be recorded by a different process.

    An approval applies to the exact arguments that were held. The runtime compares
    them before executing, so approving one hold is not a standing permission for
    the next thing the agent does.
    """
    body = (await request.body()).decode("utf-8", errors="replace")
    form = _parse_qs(body)
    hold_id = (form.get("hold_id") or [""])[0].strip()
    decision = (form.get("decision") or [""])[0].strip()

    if not hold_id:
        return HTMLResponse("hold_id is required", status_code=400)
    if decision not in ("approve", "decline"):
        # Named so a caller can see what to send rather than guessing from a 400.
        return HTMLResponse(
            f"decision must be 'approve' or 'decline', got {{decision!r}}",
            status_code=400,
        )

    built = _app()
    try:
        record = built.integration.bridge.resolve_hold(
            hold_id,
            approve=(decision == "approve"),
            decided_by="owner",
            note="decided via the approval queue",
        )
    except Exception as exc:  # unknown hold, or already decided
        # Reported rather than swallowed: a decision endpoint that returns success
        # for a hold that was not decided leaves the owner believing they approved.
        return HTMLResponse(
            f"the hold could not be decided: {{exc}}", status_code=409
        )

    note = ""
    if record.status == "approved":
        note = (
            "Approved. The held action executes with the exact arguments that were "
            "held, when its run resumes."
        )
    else:
        note = "Declined. The held action did not run, and no retry is implied."

    return _templates.get_template("_held_action.html.j2").render(
        hold=record.model_dump(mode="json"),
        note=note,
    )


@app.get("/runs", response_class=HTMLResponse)
def runs() -> str:
    """The run history, as a table."""
    return _templates.get_template("table.html.j2").render(
        app_name={json.dumps(spec.identity.name)},
        heading="Runs",
        pages=_nav("/runs"),
        columns=[{{"key": "run_id", "label": "Run"}},
                 {{"key": "agent", "label": "Agent"}},
                 {{"key": "entries", "label": "Entries", "numeric": True}}],
        rows=[],
    )


@app.get("/audit", response_class=HTMLResponse)
def audit() -> JSONResponse:
    """The audit log, newest last. Read-only: the log is append-only by design."""
    built = _app()
    path = built.audit_log_path
    if not path.exists():
        return JSONResponse({{"path": str(path), "entries": []}})
    import json as _json
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(_json.loads(line))
    return JSONResponse({{"path": str(path), "entries": entries}})


def _nav(current: str) -> list[dict[str, Any]]:
    """Navigation from the spec's declared pages.

    HREFs are relative, matching the kit's rule. A page whose id is not routed yet
    still appears, because hiding it would make a missing route look like a missing
    page.
    """
    routes = {{"/": "/", "holds": "/holds", "runs": "/runs", "audit": "/audit"}}
    nav = []
    for page_id in PAGES:
        href = routes.get(page_id, "")
        if not href:
            # TODO: this page has no route yet. Add one, or remove the page from
            # the spec's ui.pages.
            href = "#"
        nav.append({{"id": page_id, "title": page_id.replace("_", " ").title(),
                    "href": href, "current": href == current}})
    return nav
'''


# ---------------------------------------------------------------------------
# README, data/README, tests
# ---------------------------------------------------------------------------

def data_readme(spec: AppSpec) -> str:
    entities = "\n".join(
        f"* `{e.name}.json` — a JSON list of {e.name} objects, one per row. Fields: "
        + ", ".join(f"`{f.name}` ({f.type}{', required' if f.required else ''})"
                    for f in e.fields)
        for e in spec.local_entities) or "* no local entities are declared"
    return f"""# Fixture data for {spec.identity.name}

`schema.sql` is generated from `helios-app.yaml`. Do not edit it: change the spec
and regenerate, or the next run overwrites the edit.

## Fixture shape

One JSON file per entity, named after it (a trailing `s` is also accepted):

{entities}

Either a bare list of objects, or a mapping with the list under the entity's name:

```json
[{{"id": "REC-1", "...": "..."}}]
```

```json
{{"{(spec.local_entities[0].name if spec.local_entities else 'entity')}": [{{"id": "REC-1"}}]}}
```

## Rules the loader enforces

* **Money is never a float.** A `decimal` field takes a decimal string (`"12.50"`),
  a `Decimal`, or an int. A float is refused with the field named, because
  `Decimal(0.1)` bakes in binary rounding at the moment of conversion.
* **Unknown is NULL, not zero.** Omit an optional field and it stores NULL. A CHECK
  constraint on an optional column allows NULL; a required one does not.
* **Dates are ISO.** `"2026-10-15"` for a `date`, `"2026-10-15T09:30:00"` for a
  `datetime`. Digit-shaped but impossible values (`2026-13-45`) are refused.
* **Every problem is reported at once.** A row with three bad fields produces one
  error naming all three.

## Seeding

```python
import store
conn = store.connect()
store.init_db(conn)
from app_builder.schema import seed_from_json   # or copy the coercion into store.py
seed_from_json(conn, "data", entities=...)
```

A missing fixture file is skipped, not an error: a generated app ships with a
schema and no data.
"""


def app_readme(spec: AppSpec) -> str:
    gated = [t for t in spec.tools if guess_side_effect(t)]
    reads = [t for t in spec.tools if not guess_side_effect(t)]
    page_rows = "\n".join(
        f"| `{p.id}` | {p.title or p.id} | {p.pattern} | TODO |" for p in spec.pages)
    return f"""# {spec.identity.name}

{spec.identity.description.strip()}

Generated by `helios-app new` from `helios-app.yaml`. The skeleton validates clean;
the policy, the facts, the tool handlers and the scenarios are still to be written.

## What is here

| Path | What it is |
|---|---|
| `helios-app.yaml` | The spec. Everything else is generated from it. |
| `helios.yaml` | The deployment manifest the engine reads. |
| `agents/{spec.app_id}-audit.yaml` | **The policy.** The authoritative in-app gate. |
| `agents/{spec.app_id}-action.json` | The action agent's instructions and mandate. |
| `facts.py` | Trusted facts for the audit agent. |
| `tools.py` | What the agents may actually do. |
| `store.py` | Generated SQLite access layer. Parameterised SQL only. |
| `graph.py` | Cross-app publish/subscribe call sites. |
| `app.py` | Composition: the platform's gated loop over this app's policy. |
| `server.py` | ASGI entry (`server:app`) with health, dashboard, holds, runs, audit. |
| `data/schema.sql` | Generated DDL. |
| `tests/test_controls.py` | One test per control. |

## Tools

Read-only (run without a grant): {", ".join(f"`{t}`" for t in reads) or "none"}

Side-effecting (gated proposals): {", ".join(f"`{t}`" for t in gated) or "none"}

The `side_effect` flags were **guessed from the tool names** and are marked
`# REVIEW` in the audit manifest. Check them: a real side effect declared read-only
runs with no gate at all. `helios-app validate` compares the two files and fails if
they disagree.

## Write the policy first

`agents/{spec.app_id}-audit.yaml` ships with the five rule shapes as a commented
TODO block and **no active rules**. Until they are written, nothing is gated beyond
the platform's baseline. The order matters: policy before agent, because an
instructions file written first describes an agent that has no limits, and the
limits are the product.

```
helios-app validate .
```

## Scenarios

One per control, and one for each of these six outcomes. They are the evidence that
the gate works rather than that the agent is well behaved.

| Subject | Task | Control | Expects |
|---|---|---|---|
| TODO | `{spec.app_id.replace('-', '_')}.operate` | normal call, inside every limit | allowed |
| TODO | `{spec.app_id.replace('-', '_')}.operate` | over a ceiling, owner approves | held then executed with the exact held arguments |
| TODO | `{spec.app_id.replace('-', '_')}.operate` | owner declines | held, nothing happens |
| TODO | `{spec.app_id.replace('-', '_')}.operate` | refused outright | denied with the reason; the owner is never asked |
| TODO | `{spec.app_id.replace('-', '_')}.operate` | the agent misstates a value | the fact source contradicts it and nothing changes |
| TODO | `{spec.app_id.replace('-', '_')}.operate` | a tool the auditor never declared | refused |

## Pages

| Page | Title | Pattern | Route |
|---|---|---|---|
{page_rows}

## Running it

```
python3 app.py --list
python3 app.py --scenario <id>
uvicorn server:app --port 3000
```

## Deploying

`helios.yaml` names `server:app` as the entry and declares only the permissions the
engine accepts. See `platform/deployment/README.md`.
"""


def controls_tests(spec: AppSpec) -> str:
    """One parametrised placeholder per gated tool, plus the manifest invariants.

    The generated tests are the ones that can be written without knowing the policy:
    that both manifests load, that the cross-file invariant holds, and that an
    undeclared tool is refused. The six control outcomes need real policy and are
    left as named TODOs rather than as tests that pass vacuously.
    """
    gated = [t for t in spec.tools if guess_side_effect(t)]
    gated_literal = ", ".join(f'"{t}"' for t in gated) or ""
    return f'''"""Control tests for {spec.identity.name}.

The tests that can be written before the policy exists are here: both manifests
load, the cross-file invariant holds, and an undeclared tool is refused. Those are
the properties that make the gate a gate.

The six control outcomes — allowed, held-and-resumed, denied with a reason, a lying
agent caught, an undeclared tool refused, every step logged — need real rules, so
they are named below as TODOs rather than written as tests that pass vacuously. A
test that asserts nothing is worse than no test, because it reads as coverage.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]

AUDIT_MANIFEST = HERE / "agents" / "{spec.app_id}-audit.yaml"
ACTION_MANIFEST = HERE / "agents" / "{spec.app_id}-action.json"

GATED_TOOLS = [{gated_literal}]


@pytest.fixture(scope="module")
def audit():
    from audit_agent.manifest import AuditManifest
    return AuditManifest.load(AUDIT_MANIFEST)


@pytest.fixture(scope="module")
def action():
    # `validate_manifest` takes the parsed document; `load_manifest` takes a PATH.
    # Using the wrong one fails with a TypeError from pathlib rather than a
    # manifest error, which reads like a broken install.
    from agent_toolkit.manifest import validate_manifest
    return validate_manifest(json.loads(ACTION_MANIFEST.read_text(encoding="utf-8")))


def test_the_audit_manifest_loads_under_its_own_validator(audit) -> None:
    """Loads, and says which app it governs."""
    assert audit.app_id == "{spec.app_id}"


def test_the_action_manifest_loads_under_its_own_validator(action) -> None:
    assert action.name == "{spec.app_id}"
    assert action.tools


def test_the_action_manifest_rejects_an_unknown_key() -> None:
    """`extra="forbid"` semantics: even a `_comment` is refused.

    Pinned because it is the kind of thing a helpful edit adds, and the failure
    message does not obviously point at the key that caused it.
    """
    from agent_toolkit.manifest import ManifestError, validate_manifest
    document = json.loads(ACTION_MANIFEST.read_text(encoding="utf-8"))
    document["_comment"] = "harmless?"
    with pytest.raises(ManifestError):
        validate_manifest(document)


def test_every_action_tool_is_declared_by_the_auditor(audit, action) -> None:
    """THE invariant. An undeclared tool is not denied — it is never looked at,
    so it runs with no gate at all."""
    declared = {{t.name for t in audit.tools}}
    offered = set(action.tools) | set(action.mandate.allowed_tools)
    assert offered <= declared, sorted(offered - declared)


def test_the_identity_agrees_across_files(audit, action) -> None:
    assert audit.app_id == action.name == "{spec.app_id}"


def test_the_app_implements_every_declared_tool(audit) -> None:
    import tools as app_tools
    declared = {{t.name for t in audit.tools}}
    assert declared <= set(app_tools.TOOL_NAMES), sorted(declared - set(app_tools.TOOL_NAMES))


def test_the_side_effect_flags_agree_between_the_two_files(audit) -> None:
    """A tool gated in one file and ungated in the other is a hole, not a style
    difference. The unsafe direction — real side effect, `side_effect: false` —
    runs it with no proposal and no owner."""
    import tools as app_tools
    built = {{t.name: t.side_effect for t in app_tools.build_tools()}}
    for tool in audit.tools:
        if tool.name in built:
            assert bool(tool.side_effect) == bool(built[tool.name]), tool.name


@pytest.mark.parametrize("tool_name", GATED_TOOLS)
def test_a_gated_tool_declares_an_input_schema(audit, tool_name: str) -> None:
    """Without a schema, malformed arguments cannot be caught before the call."""
    tool = next(t for t in audit.tools if t.name == tool_name)
    assert tool.side_effect is True
    assert tool.input_schema


@pytest.mark.parametrize("tool_name", GATED_TOOLS)
def test_a_gated_tool_takes_a_subject_the_facts_can_resolve(audit, tool_name: str) -> None:
    """The resolver keys on subject ids only. A gated tool taking none of them
    cannot have its facts resolved, so every fact-bound rule on it fails closed."""
    from app_builder.validate import SUBJECT_KEYS
    tool = next(t for t in audit.tools if t.name == tool_name)
    properties = set((tool.input_schema or {{}}).get("properties") or {{}})
    assert properties & set(SUBJECT_KEYS), (
        f"{{tool_name}} takes no subject key (one of {{list(SUBJECT_KEYS)}}); add "
        "the argument its rules need")


def test_an_undeclared_tool_is_refused(audit) -> None:
    """The gate refuses what it has never heard of, rather than allowing it."""
    declared = {{t.name for t in audit.tools}}
    assert "definitely_not_a_real_tool" not in declared


def test_at_least_one_task_allows_the_tools_the_agent_offers(audit, action) -> None:
    """Otherwise every call is denied at authorize() time and the app looks broken
    rather than governed."""
    offered = set(action.tools) | set(action.mandate.allowed_tools)
    assert any(offered & set(t.allowed_tools) for t in audit.tasks.values())


def test_no_rule_id_claims_the_baseline_namespace(audit) -> None:
    """Baseline rules belong to the platform and cannot be redefined or disabled."""
    for rule in audit.rules:
        assert not rule.id.startswith("baseline."), rule.id


# --- TODO: one test per control, once the policy exists ---------------------
#
# def test_a_normal_call_is_allowed(...)
# def test_a_call_over_the_ceiling_is_held_and_resumes_with_the_exact_arguments(...)
# def test_a_declined_hold_does_nothing(...)
# def test_a_refused_call_is_denied_with_a_reason_and_never_reaches_the_owner(...)
# def test_a_misstated_value_changes_nothing(...)      # facts are not claims
# def test_the_fact_source_never_reads_the_request(...) # no `arguments` parameter
# def test_an_unresolvable_fact_fails_closed(...)
# def test_every_step_is_written_to_the_action_log(...)
# def test_the_audit_log_is_hash_chained(...)
# def test_a_credential_never_reaches_the_log(...)
'''


# ---------------------------------------------------------------------------
# Kit templates and static assets
# ---------------------------------------------------------------------------

def _kit_files(app_dir: Path, spec: AppSpec, stack: Stack) -> list[tuple[Path, str]]:
    """The templates and static assets to copy, as (path, text) pairs.

    Only the patterns the spec declares are copied, plus the base layout, the
    held-action partial and the static assets. Copying all six patterns into an app
    that declares two would leave four templates nothing routes to, and a reader
    would reasonably assume they were wired up.

    Files are read here rather than copied with `shutil`, so the never-overwrite
    rule applies uniformly and `--dry-run` can report them without touching disk.
    """
    from .kit import PATTERNS, kit_dir

    source = kit_dir()
    wanted: list[str] = ["base.html.j2", "_held_action.html.j2"]
    declared = {str(p.pattern) for p in spec.pages}
    for pattern in sorted(declared):
        filename = PATTERNS.get(pattern)
        if filename:
            wanted.append(filename)

    out: list[tuple[Path, str]] = []
    for name in wanted:
        path = source / name
        if not path.is_file():
            raise ScaffoldError(
                f"the UI kit is missing {name}, which the spec's declared patterns "
                f"require; this is a broken app-builder install rather than a "
                "problem with the spec")
        out.append((app_dir / "templates" / name, path.read_text(encoding="utf-8")))

    for asset in sorted((source / "static").rglob("*")):
        if asset.is_file():
            out.append((app_dir / "static" / asset.relative_to(source / "static"),
                        asset.read_text(encoding="utf-8", errors="surrogateescape")))

    return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        lines.append(current)
    return lines or [""]


def _one_line(text: str) -> str:
    return " ".join(text.split())


def load_spec(path: Path | str) -> AppSpec:
    """Load a spec for the CLI, turning `AppSpecError` into `ScaffoldError`."""
    try:
        return AppSpec.load(path)
    except AppSpecError as exc:
        raise ScaffoldError(str(exc)) from exc


def load_intake(path: Path | str) -> dict[str, Any]:
    """Load an intake file. Absent is fine; malformed is not."""
    p = Path(path)
    if not p.exists():
        raise ScaffoldError(f"no intake file at {p}; omit --intake, or write one "
                            "with `helios-app questions`")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScaffoldError(f"{p} does not parse as YAML: {_one_line(exc)}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ScaffoldError(f"{p} must be a mapping of question to answer at the "
                            "top level")
    return data

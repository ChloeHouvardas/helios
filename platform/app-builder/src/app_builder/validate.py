"""Validate a Helios app: its spec, its two manifests, and the code they point at.

    validate_app("apps/helios-collect") -> Report(errors=[...], warnings=[...], exit_code=0|1)

Two layers
----------
`app_builder.spec` is **syntax**: is this a well-formed spec. It never touches the
filesystem beyond reading the one YAML file.

This module is **semantics**: does the spec agree with the manifests and the code.
It assumes the shape is sound, so every dereference here is unguarded — a spec that
reached this point has already been through `AppSpec.parse`.

The checks are split the same way the risk is split. The single most important one
is **action-manifest tools ⊆ audit-manifest declared tools**: the audit agent is
the registry of what may be granted at all, so an action agent offering a tool the
auditor has never declared is an ungated side effect waiting to happen. Everything
else protects that property or catches a way it can be quietly lost.

Reports all problems at once, because the errors are independent and one round trip
per error through a revision loop is slow.

Fail closed
-----------
Every check that cannot reach a verdict says so, and says it as an error or a
warning rather than staying silent. A validator that returns nothing is
indistinguishable from a validator that checked nothing.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any, Iterable

from .spec import AppSpec, AppSpecError, looks_monetary

__all__ = ["Report", "validate_app", "validate_spec_only", "probe_fact_keys"]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class Report:
    """Collects problems so they can all be reported at once.

    `exit_code` is 1 when there are errors, else 0. Warnings never fail a run;
    the CLI has `--strict` for treating them as failures, because a warning that
    always fails is just a slow error.
    """

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def exit_code(self) -> int:
        return 1 if self.errors else 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def dump(self, *, strict: bool = False, stream: Any = None) -> int:
        """Print warnings then errors, and return the exit code."""
        out = stream if stream is not None else sys.stdout
        for w in self.warnings:
            print(f"  WARN  {w}", file=out)
        for e in self.errors:
            print(f"  FAIL  {e}", file=out)
        n_err, n_warn = len(self.errors), len(self.warnings)
        suffix = " — --strict treats warnings as failures" if strict and n_warn else ""
        print(f"\n{n_err} error(s), {n_warn} warning(s){suffix}", file=out)
        return 1 if n_err or (strict and n_warn) else 0


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def validate_app(app_dir: Path | str, *, strict: bool = False) -> Report:
    """Validate an app directory. Never raises for a bad app — reports instead.

    `app_dir` must contain `helios-app.yaml`. A missing or unparseable spec is a
    reported error, not an exception: the caller is a model in a revision loop and
    needs the message, not a traceback.
    """
    app_dir = Path(app_dir).resolve()
    r = Report()

    spec_path = app_dir / "helios-app.yaml"
    if not spec_path.exists():
        r.error(f"{app_dir}: no helios-app.yaml here; write the app spec to this "
                "path, or point the validator at the directory that contains it")
        return r

    try:
        spec = AppSpec.load(spec_path)
    except AppSpecError as exc:
        for problem in exc.problems:
            r.error(problem)
        # Shape problems make every cross-file check meaningless — the sections
        # they read may be absent or the wrong type. Stop here with a clear reason
        # rather than cascading into attribute errors.
        r.error("cross-file checks were skipped because the spec did not validate; "
                "fix the problems above and re-run")
        return r

    check_shape_warnings(spec, r)
    check_cross_files(app_dir, spec, r)
    return r


def validate_spec_only(spec_text: str) -> Report:
    """Validate spec text without touching the filesystem.

    The shape half of `validate_app`, exposed for tests and for callers that have
    a spec but no app directory yet.
    """
    r = Report()
    try:
        spec = AppSpec.parse(spec_text)
    except AppSpecError as exc:
        for problem in exc.problems:
            r.error(problem)
        return r
    check_shape_warnings(spec, r)
    return r


# ---------------------------------------------------------------------------
# Shape checks that warn rather than fail
# ---------------------------------------------------------------------------
#
# These come from the spec's own content, so they belong with the shape layer —
# but they are not violations. Each describes an app that is probably wrong and
# might be right, which is what a warning is for.

def check_shape_warnings(spec: AppSpec, r: Report) -> None:
    # An entity with no fields generates an empty table. Legal, and occasionally
    # what you want for a marker row, but almost always a truncated spec.
    for e in spec.local_entities:
        if not e.fields:
            r.warn(f"data.local entity {e.name!r} declares no fields; the generated "
                   "table will hold nothing but ids, so add fields or remove the "
                   "entity")

    # Money in a non-monetary type is a real financial error, not a style issue.
    #
    # Only the `_cents` naming warning can fire here, and that is worth stating
    # plainly rather than leaving a plausible-looking branch behind. `float` is
    # refused by the spec model (`FieldType` is a closed set with a specific
    # message), and `looks_monetary` now excludes every non-numeric type. So the
    # only field types that reach this loop are `decimal` and `integer` — both of
    # which are correct for money. An "is it decimal or integer" error branch
    # would be unreachable, and dead code that looks like a control is worse than
    # no code: it reads as coverage that does not exist.
    for e in spec.local_entities:
        for f in e.fields:
            if not looks_monetary(f.name, str(f.type)):
                continue
            if f.type == "integer" and "cents" not in f.name.lower():
                r.warn(f"data.local.{e.name}.{f.name}: integer money should be named "
                       f"`*_cents` so the unit is unambiguous; write `{f.name}_cents`")

    if not spec.pages and not spec.ui.align_with:
        r.warn("ui declares no pages and no align_with; the app has nothing to show, "
               "so add at least one page under `ui.pages`")

    patterns = {str(p.pattern) for p in spec.pages}
    if spec.pages and "approval_queue" not in patterns:
        r.warn("no approval_queue page. Every side-effect action can be held, so "
               "without one the agents pause invisibly and the owner discovers it "
               "when they wonder why nothing happened.")

    local_names = {e.name for e in spec.local_entities}
    for pub in spec.graph.publishes:
        if pub.entity and local_names and pub.entity not in local_names:
            r.warn(f"graph.publishes entity {pub.entity!r} is not declared in "
                   "data.local; check that it is genuinely cross-app state and not "
                   "a typo")
    if spec.graph.publishes and not spec.graph.reads:
        r.warn("graph declares publishes but no reads; an entity type nothing "
               "reads may be a mistake, or may be a genuinely new capability")


# ---------------------------------------------------------------------------
# Cross-file checks
# ---------------------------------------------------------------------------

def check_cross_files(app_dir: Path, spec: AppSpec, r: Report) -> None:
    action_rel, audit_rel = spec.agents.action_manifest, spec.agents.audit_manifest
    action_path, audit_path = app_dir / action_rel, app_dir / audit_rel

    for label, p in (("action_manifest", action_path), ("audit_manifest", audit_path)):
        if not p.exists():
            r.error(f"agents.{label}: {action_rel if label == 'action_manifest' else audit_rel} "
                    f"points at a missing file ({p}); write that manifest, or fix the "
                    "path in the spec")
    if not action_path.exists() or not audit_path.exists():
        return

    action = _load_action_manifest(action_path, action_rel, r)
    audit = _load_audit_manifest(audit_path, audit_rel, r)
    if action is None or audit is None:
        return

    declared = {t.name: t for t in audit.tools}
    action_tools = set(action.tools) | set(action.mandate.allowed_tools)

    check_undeclared_tools(action_tools, declared, audit.app_id, audit_rel, r)
    check_spec_tools_superset(spec, action_tools, declared, r)
    check_identity_agreement(spec, action, audit, r)
    check_side_effect_tools(action, audit, declared, action_tools, r)
    check_task_allowlists(audit, declared, action_tools, r)
    check_rules_against_code(app_dir, audit, declared, r)
    check_tools_agreement(app_dir, audit, declared, r)
    check_subject_keys(audit, declared, r)


def _load_action_manifest(path: Path, rel: str, r: Report) -> Any:
    """Parse the action manifest under its OWN builder's validator.

    Parsed here rather than trusting the shape, because the whole point of the
    cross-file checks is that each file is valid on its own terms before they are
    compared. A malformed manifest produces one error, not a cascade.
    """
    # Imported lazily: a spec can be shape-checked without the sibling packages
    # installed, and this module must not make them a hard dependency.
    from agent_toolkit.manifest import ManifestError, validate_manifest

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        r.error(f"action manifest {rel}: is not valid JSON ({exc}); fix the syntax — "
                "check for a trailing comma or an unquoted key")
        return None
    except OSError as exc:
        r.error(f"action manifest {rel}: cannot be read ({exc})")
        return None

    try:
        return validate_manifest(data)
    except ManifestError as exc:
        r.error(f"action manifest {rel}: is invalid — {exc}")
        return None


def _load_audit_manifest(path: Path, rel: str, r: Report) -> Any:
    from audit_agent.manifest import AuditManifest

    try:
        return AuditManifest.load(path)
    except Exception as exc:  # pydantic, PolicyError, UnknownToolError, YAMLError
        r.error(f"audit manifest {rel}: is invalid — {type(exc).__name__}: "
                f"{' '.join(str(exc).split())}")
        return None


# -- THE single most important check ---------------------------------------

def check_undeclared_tools(action_tools: set[str], declared: dict[str, Any],
                           app_id: str, audit_rel: str, r: Report) -> None:
    """An action agent may not offer a tool the auditor has never declared.

    This is the property the whole platform rests on. The audit gate decides
    per call, but it can only decide about tools it knows: an undeclared tool is
    not denied, it is not even looked at, so it runs with no gate at all.
    """
    undeclared = sorted(action_tools - set(declared))
    if undeclared:
        r.error(f"action manifest offers tool(s) the audit manifest has not declared: "
                f"{undeclared}. The audit agent is the registry of what may be granted "
                f"at all, so an undeclared tool is an ungated side effect waiting to "
                f"happen. Declare each one under `tools:` in {audit_rel}, with its "
                f"`side_effect` flag and an `input_schema`, under app_id {app_id!r}.")


def check_spec_tools_superset(spec: AppSpec, action_tools: set[str],
                              declared: dict[str, Any], r: Report) -> None:
    spec_tools = set(spec.tools)
    missing = sorted((action_tools | set(declared)) - spec_tools)
    if missing:
        r.error(f"spec `tools` is not a superset of both manifests' tools; missing: "
                f"{missing}. A tool an agent can call that the app never declared is a "
                "tool nobody reviewed — add each name to the spec's top-level `tools` "
                "list.")
    unused = sorted(spec_tools - (action_tools | set(declared)))
    if unused:
        r.warn(f"spec `tools` lists tool(s) neither manifest uses: {unused}. Harmless, "
               "but a declared tool nothing can call is usually a rename that missed "
               "one file.")


def check_identity_agreement(spec: AppSpec, action: Any, audit: Any, r: Report) -> None:
    if spec.app_id != audit.app_id:
        r.error(f"identity.id {spec.app_id!r} != audit manifest app_id "
                f"{audit.app_id!r}; two files that look like one app are two. Make "
                "them equal — identity.id is the app's slug and the audit log's "
                "principal, so a mismatch splits the app's own history.")
    if action.name and audit.app_id and action.name != audit.app_id:
        r.warn(f"action manifest name {action.name!r} differs from audit app_id "
               f"{audit.app_id!r}; not fatal, but confusing in logs and grants")


def check_side_effect_tools(action: Any, audit: Any, declared: dict[str, Any],
                            action_tools: set[str], r: Report) -> None:
    """A gated tool must carry a schema, and an ungated side effect must not exist."""
    allowed = set(action.mandate.allowed_tools)
    for tname in sorted(action_tools & set(declared)):
        t = declared[tname]
        if not (t.side_effect and tname in allowed):
            continue
        # Allowed AND side-effecting: it will be proposed. Without a schema,
        # malformed arguments cannot be caught before the call happens, and the
        # gate's `required_arguments` / `limits` have nothing to bind to.
        if not t.input_schema:
            r.error(f"tool {tname!r} is side-effecting and in the mandate but "
                    "declares no input_schema; without it malformed arguments cannot "
                    "be caught before the call happens. Add an `input_schema` with "
                    "`type: object`, `additionalProperties: false`, and the arguments "
                    "the tool takes.")
        if not any(tname in (rule.tools or []) or not rule.tools for rule in audit.rules):
            r.warn(f"side-effect tool {tname!r} has no app rule mentioning it. Baseline "
                   "rules still apply, but consider whether this app needs its own "
                   "limit.")


def check_task_allowlists(audit: Any, declared: dict[str, Any],
                          action_tools: set[str], r: Report) -> None:
    """Task allowlists must narrow what `tools:` grants, and must grant something.

    The undeclared-tool branch below is defence in depth, not the first line:
    `AuditManifest.self_check()` already raises `UnknownToolError` for a task that
    allows an undeclared tool, so in practice such a manifest fails to load and is
    reported by `_load_audit_manifest` instead. It is kept because this function is
    also reachable with a manifest built in memory rather than loaded from disk
    (which the tests do), and because a check that only works when an upstream
    validator happens to run first is not a check.

    The second branch is the one that fires in real generated apps: a task set
    that allows nothing the agent offers denies every call at authorize() time.
    """
    for task_name, task in audit.tasks.items():
        unknown = sorted(set(task.allowed_tools) - set(declared))
        if unknown:
            r.error(f"audit task {task_name!r} allows undeclared tool(s): {unknown}; "
                    "a task allowlist can only narrow what `tools:` already grants, "
                    "so declare them first")
    if audit.tasks and not any(action_tools & set(t.allowed_tools)
                               for t in audit.tasks.values()):
        r.error("no audit task allows any tool the action agent offers; the agent will "
                "be denied everything at authorize() time. Add the agent's tools to at "
                "least one task's `allowed_tools`.")


# ---------------------------------------------------------------------------
# Rules vs. the app's code
# ---------------------------------------------------------------------------

#: Fact names served by a dedicated FactSource method rather than by
#: `extra_facts_for`. The resolver checks these first, so a rule naming one is
#: satisfied without the key appearing in the extras dict.
_PROTOCOL_FACTS = {"payment_details_changed"}


def check_rules_against_code(app_dir: Path, audit: Any, declared: dict[str, Any],
                             r: Report) -> None:
    """Every fact and argument a rule names must actually exist.

    A fact-bound rule whose fact never resolves does not fail open — it fails
    closed and denies everything. That is the safe direction and exactly why it
    goes unnoticed until the demo: the app looks broken rather than unsafe. So
    this check turns "silently denies everything" into an error that names the key.
    """
    check_rule_arguments(audit, declared, r)
    check_fact_coverage(app_dir, audit, r)

def check_rule_arguments(audit: Any, declared: dict[str, Any], r: Report) -> None:
    """Every argument name a rule binds must exist in that tool's input_schema.

    A rule limiting `amount_cents` on a tool whose schema calls it `amount` does
    not error at load time. It just never matches, so the limit silently does not
    apply — the control is gone and nothing says so.
    """
    for rule in audit.rules:
        for tname in (rule.tools or []):
            tool = declared.get(tname)
            if tool is None:
                continue  # already reported by check_undeclared_tools
            props = set(_schema_properties(tool.input_schema))

            bound: list[str] = list(rule.required_arguments or [])
            bound += [lim.argument for lim in (rule.limits or []) if lim.argument]
            bound += list((rule.forbidden_values or {}).keys())

            missing = sorted({b for b in bound if b not in props})
            if missing:
                have = sorted(props) or ["<none>"]
                r.error(f"rule {rule.id!r} binds argument(s) {missing} on tool "
                        f"{tname!r}, whose input_schema declares {have}. A limit on an "
                        "argument the tool does not take never matches, so the control "
                        "silently does not apply. Rename the argument in the rule, or "
                        "add it to the tool's input_schema.")

            if not props and rule.limits:
                r.warn(f"rule {rule.id!r} limits arguments on tool {tname!r}, which "
                       "declares no input_schema properties; the limits cannot bind")


def _schema_properties(schema: Any) -> Iterable[str]:
    props = (schema or {}).get("properties") if isinstance(schema, dict) else None
    return props.keys() if isinstance(props, dict) else []


def check_fact_coverage(app_dir: Path, audit: Any, r: Report) -> None:
    """Every fact key a rule depends on must be produced by the app's fact source."""
    fact_keys: set[str] = set()
    needs_contact_count = False
    for rule in audit.rules:
        for lim in rule.limits or []:
            if lim.fact:
                fact_keys.add(lim.fact)
            if lim.max_from_fact:
                fact_keys.add(lim.max_from_fact)
        fact_keys.update(rule.deny_if_fact_true or [])
        fact_keys.update(rule.approval_if_fact_true or [])
        if rule.max_contacts_in_days:
            needs_contact_count = True

    if not fact_keys and not needs_contact_count:
        return

    facts_path = app_dir / "facts.py"
    if not facts_path.exists():
        # Not an error: an app may resolve facts another way (invoice-payer keeps
        # its fact source inline in app.py). But then the check cannot run, and
        # saying nothing would be the same as saying it passed.
        r.warn(f"rules depend on trusted fact keys {sorted(fact_keys)}"
               f"{' and on contact_count_for' if needs_contact_count else ''}, but "
               "there is no facts.py to check them against. Your FactSource must "
               "supply every one via amount_for / contact_count_for / "
               "payment_details_changed_for / extra_facts_for, or the rule fails "
               "closed and denies everything.")
        return

    keys, methods, import_error = probe_fact_keys(facts_path)
    if import_error:
        r.warn(f"facts.py could not be imported ({import_error}), so fact coverage "
               f"was not verified. The rules need these keys: {sorted(fact_keys)}")
        return

    if needs_contact_count and "contact_count_for" not in methods:
        r.error("a rule sets max_contacts_in_days but facts.py defines no "
                "contact_count_for; the frequency limit cannot resolve and will deny "
                "every call. Implement contact_count_for(subject_id, within_days) "
                "returning an int, or None for an unknown subject.")

    supplied = keys | _PROTOCOL_FACTS
    missing = sorted(fact_keys - supplied)
    if missing:
        r.error(f"rules depend on fact key(s) {missing} that facts.py never produces. "
                "An unresolvable fact is not ignored — it fails closed and denies "
                "every call the rule covers, so the app looks broken at demo time. "
                "Return each key from extra_facts_for(subject_id), or remove the rule "
                f"that names it. facts.py currently produces {sorted(keys) or 'nothing'}.")


def probe_fact_keys(facts_path: Path) -> tuple[set[str], set[str], str | None]:
    """The fact keys `facts.py` can produce, and the FactSource methods it defines.

    Returns `(keys, methods, import_error)`.

    Why this is static analysis and not a call
    ------------------------------------------
    The obvious implementation — import `facts.py` and call
    `extra_facts_for("dummy")` — returns an EMPTY dict for a correct app. Fact
    sources resolve a subject id against the store and return `None`/`{}` for one
    they do not know, which is required behaviour: unknown must be None, never
    zero, or a fail-closed control becomes fail-open. So a dummy subject produces
    no keys, every key looks missing, and the validator reports errors against an
    app that is right.

    The key set is therefore read from the AST: every string literal used as a
    dict key or subscript target inside `extra_facts_for`. That is a superset of
    what any single call can return, which is the correct question — "could this
    key ever be produced" — rather than "was it produced for a subject I made up".

    The module is still imported, in a subprocess, for two reasons: it proves
    `facts.py` imports at all (a syntax error or a missing dependency is a real
    finding), and the module namespace resolves constant names the AST alone
    cannot. The subprocess is deliberate isolation — importing executes the
    module's top-level code, and a crash, an `os._exit`, or a stray print must not
    take the validator down with it.
    """
    keys: set[str] = set()
    methods: set[str] = set()

    try:
        source = facts_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(facts_path))
    except (OSError, SyntaxError) as exc:
        return keys, methods, f"{' '.join(str(exc).split())}"

    functions: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, node)
            if node.name in _FACT_SOURCE_METHODS:
                methods.add(node.name)

    # extra_facts_for plus every module-level function or method it calls,
    # transitively. A fact source that builds `{"supplier_ceiling": ...}` in a
    # `_supplier_facts` helper and merges it produces that key just as surely as
    # one that writes it inline, and reporting it as missing would send the
    # author inlining code to satisfy a probe.
    entry = functions.get("extra_facts_for")
    if entry is not None:
        seen: set[str] = set()
        pending = [entry]
        while pending:
            fn = pending.pop()
            if fn.name in seen:
                continue
            seen.add(fn.name)
            keys |= _fact_keys_in(fn)
            for callee in _called_names(fn):
                target = functions.get(callee)
                if target is not None and callee not in seen:
                    pending.append(target)

    # Importing is a smoke test. If it fails, the AST result is still usable and
    # more informative than nothing, so the error is returned alongside rather
    # than instead.
    import_error = _import_module(facts_path)[1]
    return keys, methods, import_error


_FACT_SOURCE_METHODS = frozenset({
    "amount_for", "contact_count_for", "counterparty_for",
    "payment_details_changed_for", "extra_facts_for", "record_contact",
})


def _called_names(fn: ast.FunctionDef) -> set[str]:
    """Names of functions and methods called inside `fn` (`f(...)`, `self.f(...)`)."""
    names: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _fact_keys_in(fn: ast.FunctionDef) -> set[str]:
    """Every string literal that can become a fact key inside this function.

    Covers the shapes real fact sources use: `facts["k"] = v`, `facts = {"k": v}`,
    `return {"k": v}`, `facts.update({"k": v})` and `facts.setdefault("k", v)`.
    Anything else (a key built by formatting or a loop) is invisible here, which
    is why a miss is reported as an error naming what WAS found — the fix is then
    obvious either way.
    """
    keys: set[str] = set()

    for node in ast.walk(fn):
        # facts["k"] = ...
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
                    if isinstance(target.slice.value, str):
                        keys.add(target.slice.value)
        # {"k": v} — covers dict literals, returns, and update({...})
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
        # facts.setdefault("k", v)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setdefault" and node.args):
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                keys.add(first.value)

    return keys


_IMPORT_SCRIPT = """
import importlib.util, json, pathlib, sys
app_dir = pathlib.Path({app_dir!r})
target = pathlib.Path({target!r})
sys.path.insert(0, str(app_dir))
spec = importlib.util.spec_from_file_location(target.stem, target)
mod = importlib.util.module_from_spec(spec)
sys.modules[target.stem] = mod
spec.loader.exec_module(mod)
print(json.dumps({{k: v for k, v in vars(mod).items()
                  if not k.startswith('__')
                  and isinstance(v, (str, int, float, bool))}}))
"""


def _import_module(module_path: Path, *, timeout: float = 30.0
                   ) -> tuple[dict[str, Any] | None, str | None]:
    """Import an app module in a subprocess. Returns `(namespace, error)`.

    Exactly one of the two is meaningful: on success `namespace` holds the
    module-level scalars (so `name=SEND_EMAIL` resolves to a real string) and
    `error` is None; on failure `error` explains why and `namespace` is None.

    Why a subprocess rather than an import
    --------------------------------------
    Importing executes the module's top-level code. An app module may build a
    store, open a database, or crash — and a validator that dies on the thing it
    is validating cannot report anything. Isolating it means a segfault, an
    `os._exit`, a stray print or an infinite loop all become one warning naming
    the file, and validation of everything else continues.

    The app directory goes on `sys.path` because app modules import each other by
    bare name (`from book import Book`), which only resolves with the app
    directory as the working root — the same reason `test-all.sh` cds into each
    suite before running it.

    Run with `-B` so no `__pycache__` is written into the directory being
    validated. `specs/app-builder.md` calls the validator "cheap, side-effect
    free", and a read-only check that litters bytecode into someone else's app
    directory is not that — it would also fail outright on a read-only checkout.
    """
    # Resolve FIRST. The subprocess runs with cwd=app_dir, so a relative path
    # handed in by the caller would be resolved a second time against that new
    # cwd and come out doubled (apps/x/apps/x/facts.py).
    module_path = module_path.resolve()
    app_dir = module_path.parent
    script = _IMPORT_SCRIPT.format(app_dir=str(app_dir), target=str(module_path))
    try:
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-B", "-c", script],
            capture_output=True, text=True, timeout=timeout, cwd=str(app_dir),
        )
    except subprocess.TimeoutExpired:
        return None, f"importing {module_path.name} timed out after {timeout:.0f}s"
    except OSError as exc:
        return None, f"could not start an interpreter to import {module_path.name}: {exc}"

    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        return None, (" ".join(tail[-1].split()) if tail
                      else f"importing {module_path.name} exited {proc.returncode}")

    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        # The module imported but did not print its namespace — e.g. it writes to
        # stdout itself. Not an error worth failing on; just no constants.
        return None, None
    return (data if isinstance(data, dict) else None), None


# ---------------------------------------------------------------------------
# tools.py agreement
# ---------------------------------------------------------------------------

def check_tools_agreement(app_dir: Path, audit: Any, declared: dict[str, Any],
                          r: Report) -> None:
    """Audit `side_effect` flags must agree with the app's own `ToolDefinition`s.

    Both directions matter, and they are not equally bad:

      * auditor says `side_effect: false`, app says `true` — the app builds a
        gated tool that the gate waves through as a read. The side effect runs
        with no proposal, no hold and no owner. This is the dangerous one.
      * auditor says `true`, app says `false` — the app's read path is gated, so
        it is slow and annoying but safe.

    The message names the direction rather than reporting a bare mismatch,
    because the fix differs: one is a gate bypass and one is a nuisance.
    """
    tools_path = app_dir / "tools.py"
    if not tools_path.exists():
        return  # an app may define tools elsewhere; nothing to compare against

    found, import_error = probe_tool_definitions(tools_path)
    if import_error:
        r.warn(f"tools.py could not be imported ({import_error}), so its "
               "side_effect flags were not compared with the audit manifest")
        return

    if not found:
        r.warn("tools.py constructs no ToolDefinition the validator can see; if the "
               "tools come from the platform (e.g. fin_tools' bridge or "
               "agent_toolkit.tools) this is expected, otherwise the side_effect "
               "flags cannot be cross-checked")
        return

    for name, side_effect in sorted(found.items()):
        tool = declared.get(name)
        if tool is None:
            # Not reported here: check_undeclared_tools covers tools the ACTION
            # manifest offers. A tool defined in tools.py but declared nowhere is
            # worth a nudge, not an error — the app may not have wired it yet.
            r.warn(f"tools.py defines {name!r} but the audit manifest does not "
                   "declare it; an undeclared tool cannot be granted, so either "
                   "declare it or remove the definition")
            continue
        if bool(tool.side_effect) != bool(side_effect):
            if not tool.side_effect and side_effect:
                r.error(f"tool {name!r} is side_effect=True in tools.py but "
                        "side_effect: false in the audit manifest. The gate treats it "
                        "as a read and runs it with no proposal and no hold — an "
                        "ungated side effect. Set `side_effect: true` for this tool in "
                        "the audit manifest.")
            else:
                r.error(f"tool {name!r} is side_effect=False in tools.py but "
                        "side_effect: true in the audit manifest. The two files "
                        "disagree about whether this call needs a gate, so one of them "
                        "is wrong; make them agree. If it really is a read, set "
                        "`side_effect: false` in the audit manifest.")


def probe_tool_definitions(tools_path: Path) -> tuple[dict[str, bool], str | None]:
    """`(name -> side_effect)` for every `ToolDefinition(...)` in the file.

    Read from the AST rather than by importing and enumerating, because real apps
    build their tools inside factory functions that need a store and a bundle:
    `make_record_payment(book, bundle.payments, bundle.graph)`. Nothing can be
    enumerated from module globals without constructing the whole app.

    The module is imported once, in a subprocess, to resolve constant names
    (`name=SEND_EMAIL`) against the real namespace and to prove the file imports.
    Platform-provided tools (helios-collect reuses `make_send_email`) are
    invisible here by construction, which is why a missing tool is a warning in
    the caller and not an error.
    """
    found: dict[str, bool] = {}
    try:
        source = tools_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(tools_path))
    except (OSError, SyntaxError) as exc:
        return found, " ".join(str(exc).split())

    # Module-level constants, so `name=SEND_EMAIL` resolves without importing.
    consts: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and isinstance(node.value, ast.Constant):
                    consts[t.id] = node.value.value

    # One import for both purposes: namespace resolution and the smoke test.
    namespace, import_error = _import_module(tools_path)
    if namespace:
        consts.update(namespace)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "ToolDefinition"):
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        name = _resolve(kw.get("name"), consts)
        if not isinstance(name, str) or not name:
            continue
        side = _resolve(kw.get("side_effect"), consts)
        found[name] = bool(side)

    return found, import_error


def _resolve(node: ast.expr | None, consts: dict[str, Any]) -> Any:
    """Resolve a keyword's AST node to a value, following module constants."""
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _resolve(node.operand, consts)
        return None if inner is None else not inner
    return None


# ---------------------------------------------------------------------------
# Subject keys
# ---------------------------------------------------------------------------

#: Copied rather than imported so this module keeps working without audit_agent
#: installed. The value is asserted against the real constant in tests, so a
#: change there fails loudly here instead of silently drifting.
SUBJECT_KEYS = ("invoice_id", "bill_id", "customer_id", "supplier_id",
                "subject_id", "payment_id")


def check_subject_keys(audit: Any, declared: dict[str, Any], r: Report) -> None:
    """A gated tool with no subject-key argument cannot have its facts resolved.

    The resolver keys on SUBJECT_KEYS only. A tool that takes none of them gives
    the auditor no way to find the record it should be checking against, so every
    fact-bound rule on it fails closed. That is safe and useless: the tool can
    never be approved, and the reason is not visible from the manifest.

    Reported once per tool, not once per rule. A tool gated by four fact-bound
    rules has one problem, and four copies of it bury the other findings — which
    is the same failure mode as a checker that cries wolf.
    """
    offenders: dict[str, list[str]] = {}
    for rule in audit.rules:
        if not (rule.limits or rule.deny_if_fact_true or rule.approval_if_fact_true
                or rule.max_contacts_in_days):
            continue
        for tname in (rule.tools or []):
            tool = declared.get(tname)
            if tool is None or not tool.side_effect:
                continue
            props = set(_schema_properties(tool.input_schema))
            if props and not (props & set(SUBJECT_KEYS)):
                offenders.setdefault(tname, []).append(rule.id)

    for tname, rule_ids in sorted(offenders.items()):
        r.warn(f"tool {tname!r} is gated by rule(s) {sorted(set(rule_ids))} but its "
               f"input_schema has no subject key (one of {list(SUBJECT_KEYS)}); facts "
               "will not resolve for this tool, so those rules fail closed and deny "
               "every call. Add the subject argument the rules need.")

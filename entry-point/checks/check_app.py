#!/usr/bin/env python3
"""check_app.py — validate a generated Helios app before you demo it.

    python3 entry-point/checks/check_app.py entry-point/examples/invoice-payer
    python3 entry-point/checks/check_app.py apps/your-app --strict

Why this exists
---------------
`specs/app-builder.md` defines a validator that does not exist yet: no
`platform/app-builder/` code, no pydantic models for `helios-app.yaml`, no
`stacks.yaml`. `BUILD.md` therefore tells you to self-check the invariants by
hand, which is exactly the kind of thing that gets skipped at hour 70.

This script is that self-check, mechanised. It implements the checks
`specs/app-builder.md` specifies under "Validator", using the two packages that
*do* exist as the real loaders for the two manifests. It reports **all** problems
at once rather than failing on the first, because the errors are independent and
one round trip per error is slow.

It is a substitute, not the real thing. When the app-builder validator lands,
delete this and use that. Until then, run it in CI.

What it checks
--------------
Shape (spec only):
  - parses as YAML; every documented section present; no unknown top-level key
  - exactly one of stack.use / stack.match_existing
  - exactly one of data.local / data.integration
  - identity.id is kebab-case
  - every ui page pattern is a known pattern
  - every entity field type is in the closed set
  - money-shaped fields are decimal or integer cents, never float
  - no entity appears in both data.local and data.integration
  - match_existing / integration / align_with require the matching `existing` key

Cross-file (the ones a single file cannot express):
  - both manifest paths exist and parse under their OWN builders' validators
  - action-manifest tools SUBSET OF audit-manifest declared tools   <- the big one
  - the spec's `tools` is a superset of both manifests' tool lists
  - identity.id equals the audit manifest's app_id
  - every side-effect tool the action agent may call is declared side-effecting
    in the audit manifest (a tool gated in one file and ungated in the other is
    reported, not passed)
  - every entity the spec publishes to the graph is a real entity it declares
  - every audit rule's fact / max_from_fact key is actually supplied by the app's
    fact source (a fact-bound rule whose fact never resolves denies everything)

Exit code 0 = clean, 1 = errors, 2 = usage problem. Warnings never fail the run
unless --strict.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
for _p in (
    REPO / "platform" / "audit-agent-builder" / "src",
    REPO / "platform" / "action-agent-builder" / "src",
    REPO / "platform" / "financial-tools",
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Closed sets from specs/app-builder.md. Kept here rather than imported, because
# the pydantic models that would define them do not exist yet.
UI_PATTERNS = {"summary", "table", "detail", "form", "approval_queue", "settings"}
FIELD_TYPES = {"string", "integer", "decimal", "date", "datetime", "boolean", "json"}
SPEC_SECTIONS = {"spec_version", "identity", "existing", "stack", "data", "graph",
                 "agents", "ui", "tools"}
# A field is "money-shaped" if its name suggests currency. float is never correct
# for money: a rounding error in an invoice total is a real financial error.
#
# Matched on whole words, not substrings, and never against date/datetime types.
# The naive substring version flagged `due_date` as monetary because it contains
# "due" — a false positive that trains people to ignore the checker, which is
# worse than not having one.
MONEY_WORDS = frozenset({
    "amount", "amounts", "balance", "balances", "money", "price", "prices",
    "total", "totals", "cost", "costs", "fee", "fees", "value", "values",
    "subtotal", "credit", "debit", "payment", "paid", "owed", "due_amount",
})
NON_MONETARY_TYPES = {"date", "datetime", "boolean", "json"}


def looks_monetary(field_name: str, field_type: Any) -> bool:
    """True if a field name suggests money and its type could carry money."""
    if field_type in NON_MONETARY_TYPES:
        return False
    words = set(re.split(r"[^a-z0-9]+", field_name.lower())) - {"", "cents"}
    return bool(words & MONEY_WORDS)
KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class Report:
    """Collects problems so they can all be reported at once."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def dump(self, strict: bool) -> int:
        for w in self.warnings:
            print(f"  WARN  {w}")
        for e in self.errors:
            print(f"  FAIL  {e}")
        n_err, n_warn = len(self.errors), len(self.warnings)
        suffix = " — --strict treats warnings as failures" if strict and n_warn else ""
        print(f"\n{n_err} error(s), {n_warn} warning(s){suffix}")
        return 1 if n_err or (strict and n_warn) else 0


def _load_yaml(path: Path) -> Any:
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Shape checks
# ---------------------------------------------------------------------------

def check_shape(spec: dict[str, Any], r: Report) -> None:
    unknown = sorted(set(spec) - SPEC_SECTIONS)
    if unknown:
        r.error(f"unknown top-level key(s): {unknown}; allowed: {sorted(SPEC_SECTIONS)}")
    missing = sorted(SPEC_SECTIONS - set(spec) - {"existing", "graph"})
    if missing:
        r.error(f"missing required section(s): {missing}")

    ident = spec.get("identity") or {}
    app_id = ident.get("id")
    if not app_id or not isinstance(app_id, str):
        r.error("identity.id is required and must be a non-empty string")
    elif not KEBAB.match(app_id):
        r.error(f"identity.id must be kebab-case (lowercase letters, digits, "
                f"hyphens), got {app_id!r}")

    # --- stack: exactly one of use / match_existing ----------------------
    stack = spec.get("stack") or {}
    has_use, has_match = "use" in stack, "match_existing" in stack
    if has_use == has_match:
        r.error("stack must set exactly one of `use` or `match_existing` "
                f"(got use={has_use}, match_existing={has_match})")

    # --- data: exactly one of local / integration ------------------------
    data = spec.get("data") or {}
    has_local, has_integ = "local" in data, "integration" in data
    if has_local == has_integ:
        r.error("data must set exactly one of `local` or `integration` "
                f"(got local={has_local}, integration={has_integ})")

    # --- existing must back the three integration options ----------------
    existing = spec.get("existing") or {}
    if has_match and not existing.get("stack"):
        r.error("stack.match_existing requires a populated existing.stack — "
                "matching nothing is not matching")
    if has_integ and not existing.get("data"):
        r.error("data.integration requires a populated existing.data")
    ui = spec.get("ui") or {}
    if ui.get("align_with") and not existing.get("ui"):
        r.error("ui.align_with requires a populated existing.ui")

    # --- entities, field types, money -----------------------------------
    local_names: set[str] = set()
    integ_names: set[str] = set()
    for ent in (data.get("local") or {}).get("entities") or []:
        name = ent.get("name")
        if not name:
            r.error("data.local entity is missing a name")
            continue
        local_names.add(name)
        fields = ent.get("fields") or []
        if not fields:
            r.warn(f"entity {name!r} declares no fields")
        for f in fields:
            fname, ftype = f.get("name"), f.get("type")
            if not fname:
                r.error(f"entity {name!r}: field is missing a name")
                continue
            # One message per defect. `float` is both outside the closed set and
            # wrong for money, but reporting it three times buries the other
            # errors — and a checker people skim is a checker people ignore.
            if ftype == "float":
                r.error(f"entity {name!r} field {fname!r}: money is never float — "
                        "use decimal, or integer cents")
            elif ftype not in FIELD_TYPES:
                r.error(f"entity {name!r} field {fname!r}: type {ftype!r} is not "
                        f"in the closed set {sorted(FIELD_TYPES)}")
            elif looks_monetary(fname, ftype):
                if ftype not in {"decimal", "integer"}:
                    r.error(f"entity {name!r} field {fname!r} looks monetary but is "
                            f"typed {ftype!r}; money must be decimal or integer cents")
                elif ftype == "integer" and "cents" not in fname.lower():
                    r.warn(f"entity {name!r} field {fname!r}: integer money should be "
                           "named *_cents so the unit is unambiguous")
    for n in (data.get("integration") or {}).get("entities") or []:
        integ_names.add(n)

    both = sorted(local_names & integ_names)
    if both:
        r.error(f"entity/entities in both data.local and data.integration: {both}. "
                "Data the app does not own is not created by it.")

    # --- ui patterns -----------------------------------------------------
    pages = ui.get("pages") or []
    if not pages and not ui.get("align_with"):
        r.warn("ui declares no pages and no align_with — the app has nothing to show")
    for p in pages:
        pid, pat = p.get("id"), p.get("pattern")
        if not pid:
            r.error("ui page is missing an id")
        if pat not in UI_PATTERNS:
            r.error(f"ui page {pid!r}: pattern {pat!r} is not a known pattern "
                    f"{sorted(UI_PATTERNS)}")

    if pages and "approval_queue" not in {p.get("pattern") for p in pages}:
        r.warn("no approval_queue page. Every side-effect action can be held, so "
               "without one the agents pause invisibly and the user discovers it "
               "when they wonder why nothing happened.")

    # --- graph declarations are real entities ---------------------------
    graph = spec.get("graph") or {}
    publishes = graph.get("publishes") or []
    for pub in publishes:
        entity = pub.get("entity")
        if entity and local_names and entity not in local_names:
            r.warn(f"graph.publishes entity {entity!r} is not declared in "
                   "data.local — check that it is genuinely cross-app state and "
                   "not a typo")
    if publishes and not (graph.get("reads") or []):
        r.warn("graph declares publishes but no reads: an entity type nothing "
               "reads may be a mistake, or may be a genuinely new capability")


# ---------------------------------------------------------------------------
# Cross-file checks
# ---------------------------------------------------------------------------

def check_cross_files(app_dir: Path, spec: dict[str, Any], r: Report) -> None:
    agents = spec.get("agents") or {}
    action_rel = agents.get("action_manifest")
    audit_rel = agents.get("audit_manifest")
    if not action_rel or not audit_rel:
        r.error("agents.action_manifest and agents.audit_manifest are both required")
        return

    action_path, audit_path = app_dir / action_rel, app_dir / audit_rel
    for label, p in (("action_manifest", action_path), ("audit_manifest", audit_path)):
        if not p.exists():
            r.error(f"agents.{label} points at a missing file: {p}")
    if not action_path.exists() or not audit_path.exists():
        return

    # --- parse each under its OWN builder's validator --------------------
    from agent_toolkit.manifest import ManifestError, validate_manifest
    from audit_agent.manifest import AuditManifest

    try:
        action = validate_manifest(
            json.loads(action_path.read_text(encoding="utf-8")))
    except (ManifestError, json.JSONDecodeError) as exc:
        r.error(f"action manifest {action_rel} is invalid: {exc}")
        action = None

    try:
        audit = AuditManifest.load(audit_path)
    except Exception as exc:  # AuditManifest raises pydantic + custom errors
        r.error(f"audit manifest {audit_rel} is invalid: {type(exc).__name__}: {exc}")
        audit = None

    if action is None or audit is None:
        return

    declared = {t.name: t for t in audit.tools}
    action_tools = set(action.tools) | set(action.mandate.allowed_tools)

    # --- THE single most important check --------------------------------
    undeclared = sorted(action_tools - set(declared))
    if undeclared:
        r.error(f"action manifest offers tool(s) the audit manifest has not "
                f"declared: {undeclared}. The audit agent is the registry of what "
                f"may be granted at all, so this is an ungated side effect waiting "
                f"to happen. Declare them in {audit.app_id}'s audit manifest.")

    # --- spec.tools is a superset of both -------------------------------
    spec_tools = set(spec.get("tools") or [])
    missing_from_spec = sorted((action_tools | set(declared)) - spec_tools)
    if missing_from_spec:
        r.error(f"spec `tools` is not a superset of both manifests' tools; "
                f"missing: {missing_from_spec}. A tool an agent can call that the "
                f"app never declared is a tool nobody reviewed.")
    unused = sorted(spec_tools - (action_tools | set(declared)))
    if unused:
        r.warn(f"spec `tools` lists tool(s) neither manifest uses: {unused}")

    # --- identity agreement ---------------------------------------------
    app_id = (spec.get("identity") or {}).get("id")
    if app_id and app_id != audit.app_id:
        r.error(f"identity.id {app_id!r} != audit manifest app_id "
                f"{audit.app_id!r} — two files that look like one app are two")
    if action.name and audit.app_id and action.name != audit.app_id:
        r.warn(f"action manifest name {action.name!r} differs from audit app_id "
               f"{audit.app_id!r}; not fatal, but confusing in logs and grants")

    # --- a gated tool in one file must be gated in the other ------------
    for tname in sorted(action_tools & set(declared)):
        t = declared[tname]
        if t.side_effect and tname in set(action.mandate.allowed_tools):
            # Allowed AND side-effecting: it will be proposed. Fine — but it
            # must have a schema, or malformed arguments cannot be caught
            # before the call happens.
            if not t.input_schema:
                r.error(f"tool {tname!r} is side-effecting but declares no "
                        "input_schema. Without it, malformed arguments cannot be "
                        "caught before the call happens.")
            if not any(tname in (rule.tools or []) or not rule.tools
                       for rule in audit.rules):
                r.warn(f"side-effect tool {tname!r} has no app rule mentioning it. "
                       "Baseline rules still apply, but consider whether this app "
                       "needs its own limit.")

    # --- task allowlists are a subset of declared tools -----------------
    for task_name, task in audit.tasks.items():
        unknown = sorted(set(task.allowed_tools) - set(declared))
        if unknown:
            r.error(f"audit task {task_name!r} allows undeclared tool(s): {unknown}")
    if not any(action_tools & set(t.allowed_tools) for t in audit.tasks.values()):
        r.error("no audit task allows any tool the action agent offers — the agent "
                "will be denied everything at authorize() time")

    # --- facts the rules depend on must be resolvable -------------------
    _check_fact_coverage(audit, r)


def _check_fact_coverage(audit: Any, r: Report) -> None:
    """A fact-bound rule whose fact never resolves denies everything.

    This is the most common way a generated app's policy silently stops working,
    and it fails in the safe direction — which is exactly why it is easy to miss
    until the demo. We can only check the *names* here; whether the app's fact
    source supplies them is a runtime property. So this is a warning that names
    what to implement, not a hard failure.
    """
    fact_keys: set[str] = set()
    for rule in audit.rules:
        for lim in rule.limits or []:
            if getattr(lim, "fact", None):
                fact_keys.add(lim.fact)
            if getattr(lim, "max_from_fact", None):
                fact_keys.add(lim.max_from_fact)
        for flag in (rule.deny_if_fact_true or []):
            fact_keys.add(flag)
        for flag in (rule.approval_if_fact_true or []):
            fact_keys.add(flag)
        if rule.max_contacts_in_days:
            fact_keys.add("(contact_count_for)")

    if fact_keys:
        r.warn("rules depend on these trusted fact keys — your FactSource must "
               "supply every one of them via amount_for / extra_facts_for / "
               "payment_details_changed_for / contact_count_for, or the rule "
               f"fails closed and denies everything: {sorted(fact_keys)}")


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("app_dir", help="directory containing helios-app.yaml")
    parser.add_argument("--strict", action="store_true",
                        help="treat warnings as failures")
    args = parser.parse_args(argv)

    app_dir = Path(args.app_dir).resolve()
    spec_path = app_dir / "helios-app.yaml"
    if not spec_path.exists():
        print(f"no helios-app.yaml in {app_dir}")
        return 2

    print(f"checking {app_dir.relative_to(REPO) if REPO in app_dir.parents else app_dir}")
    r = Report()

    try:
        spec = _load_yaml(spec_path)
    except Exception as exc:
        print(f"  FAIL  helios-app.yaml does not parse as YAML: {exc}")
        return 1
    if not isinstance(spec, dict):
        print("  FAIL  helios-app.yaml must be a mapping at the top level")
        return 1

    check_shape(spec, r)
    check_cross_files(app_dir, spec, r)
    return r.dump(args.strict)


if __name__ == "__main__":
    raise SystemExit(main())

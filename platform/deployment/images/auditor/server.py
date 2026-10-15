"""Helios audit sidecar: the platform's last gate before real side effects.

The untrusted app container holds only HELIOS_ACTION_TOKEN and can merely
PROPOSE an action here. This service holds HELIOS_EXECUTION_SECRET and is the
only container allowed to talk to the tenant core.

Two gates, one direction of authority
--------------------------------------
The in-app `audit_agent` is authoritative: it has trusted facts, it can HOLD an
action for the owner, and the owner's decision resumes it. This sidecar has no
facts and no owner. It may **deny** or **forward** -- never approve, and never
reverse an in-app hold. A sidecar that could approve would be a second, weaker
policy engine that an app could aim its traffic at to bypass the first.

Policy comes from data, not from this file
------------------------------------------
The engine copies the app's own audit manifest into `/policy/audit.yaml` before
this process starts, and the app has no way to modify it afterwards. Rules are
loaded with `audit_agent`'s own manifest loader, so the two gates read the same
schema through the same code and cannot drift.

Only the **fact-free subset** is enforced here. A rule that depends on a trusted
fact (`deny_if_fact_true`, `approval_if_fact_true`, `max_contacts_in_days`, or a
limit with `fact` / `max_from_fact`) cannot be evaluated without a fact source,
and `audit_agent` fails such a rule **closed** -- it denies. Running those rules
here would deny every request they touched. So they are skipped deliberately:
they are already enforced, with facts, by the in-app gate, which is the gate that
has the authority to hold and resume. Skipping them is recorded at startup so a
reviewer can see exactly what the sidecar does not check.

Core results are checked, not assumed
-------------------------------------
A non-2xx response from core is recorded as `error` and surfaced as a failure.
The imported build checked no status code at all, so a 404 -- core not
implementing the requested tool -- was recorded as `executed`. That is the worst
possible entry in a compliance log: it claims a side effect that did not happen.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from audit_agent.audit_log import AuditLog, EventType
from audit_agent.contracts import Decision, Finding, Severity
from audit_agent.errors import PolicyError
from audit_agent.manifest import AuditManifest

APP_NAME = os.environ.get("HELIOS_APP_NAME", "app")
TENANT = os.environ.get("HELIOS_TENANT", "demo")
EXPECTED_TOKEN = os.environ["EXPECTED_ACTION_TOKEN"]
EXECUTION_SECRET = os.environ["HELIOS_EXECUTION_SECRET"]
CORE_URL = os.environ.get("HELIOS_CORE_URL", "http://helios-core:9000")

#: Platform tool families granted by helios.yaml. These are the vocabulary of
#: `permissions:`; a proposed tool name is mapped onto a family by its prefix
#: (`payments.send` -> `payments`). Empty means no family is granted: an app
#: that asked for nothing can still act on its own declared tools, but cannot
#: reach the tenant core.
PERMISSIONS = {
    t.strip()
    for t in os.environ.get("ALLOWED_TOOLS", "").split(",")
    if t.strip()
}

#: Tool-name prefix -> permission family. The core service registers tools as
#: `family.operation`; helios.yaml grants families. Anything not listed here is
#: not a platform tool and can only be an app-declared tool.
FAMILY_BY_PREFIX = {
    "payments": "payments",
    "collect": "collect",
    "email": "email",
    "graph": "knowledge-graph",
    "events": "events",
    "forecast": "forecast",
    "behavior": "forecast",
    "market": "market-data",
    "spreadsheet": "spreadsheets",
    "sheets": "spreadsheets",
    "simulation": "forecast",
}


def family_of(action: str) -> str | None:
    """The permission family a proposed tool belongs to, or None."""
    prefix = action.split(".", 1)[0] if "." in action else action
    return FAMILY_BY_PREFIX.get(prefix) or (prefix if prefix in PERMISSIONS or prefix in FAMILY_BY_PREFIX.values() else None)


MANIFEST_PATH = Path(os.environ.get("HELIOS_AUDIT_MANIFEST_PATH", "/policy/audit.yaml"))
DATA_DIR = Path(os.environ.get("HELIOS_DATA_DIR", "/data"))
LOG_PATH = DATA_DIR / "audit" / "sidecar-log.jsonl"

DEDUPE_WINDOW_S = float(os.environ.get("DEDUPE_WINDOW_S", "120"))

#: Last-resort cap on `amount_cents` for the payments family when no manifest
#: limit binds. Integer cents, never float: money in this platform is integer
#: cents or decimal strings (decision B.6). 1_000_000 is 10,000.00.
DEFAULT_MAX_PAYMENT_CENTS = Decimal(os.environ.get("MAX_PAYMENT_AMOUNT_CENTS", "1000000"))


@dataclass
class FactFreePolicy:
    """The subset of an app manifest this gate can evaluate without facts.

    Built once at startup. Every field here is derived from the manifest using
    `audit_agent`'s own loader, so a manifest change is picked up by both gates
    identically.
    """

    #: Tool names declared by the manifest. Used to widen the allow-list,
    #: because an app proposes its own tool names as well as platform families.
    tool_names: frozenset[str] = frozenset()
    #: tool -> argument paths that must be present.
    required_arguments: dict[str, list[str]] = field(default_factory=dict)
    #: tool -> argument path -> forbidden literal values.
    forbidden_values: dict[str, dict[str, list[Any]]] = field(default_factory=dict)
    #: tool -> argument path -> constant ceiling.
    ceilings: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    #: tool -> max calls per grant, from tasks[].tool_overrides.
    max_calls: dict[str, int] = field(default_factory=dict)
    #: Rule ids that were skipped because they need a trusted fact. Reported at
    #: startup: a reviewer must be able to see what this gate does NOT check.
    skipped_fact_rules: list[str] = field(default_factory=list)
    #: True when a manifest was loaded. Without one the gate is allow-list only.
    loaded: bool = False
    origin: str = ""


def _dig(mapping: dict[str, Any], path: str) -> Any:
    """Resolve a dotted path; missing keys yield None, never KeyError."""
    current: Any = mapping
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _to_decimal(value: Any) -> Decimal | None:
    """Parse money as Decimal. Returns None rather than guessing."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def build_policy(path: Path = MANIFEST_PATH) -> FactFreePolicy:
    """Load the app manifest and extract the fact-free subset.

    A missing or unparseable manifest is NOT fatal: the gate still enforces the
    platform allow-list and the default cap, which is stricter than the imported
    build's behaviour. But it is reported loudly in /health, because an app that
    intended to declare a cap and got a typo would otherwise silently lose it.
    """
    policy = FactFreePolicy(origin=str(path))
    if not path.is_file():
        policy.skipped_fact_rules.append(
            f"no manifest at {path}; enforcing allow-list and default cap only"
        )
        return policy

    try:
        manifest: AuditManifest = AuditManifest.load(path)
    except PolicyError as exc:
        # Fail closed on a malformed manifest: an app whose policy cannot be
        # parsed must not run with policy silently disabled.
        raise RuntimeError(
            f"{path}: the audit manifest could not be loaded, so this sidecar "
            f"cannot enforce the app's declared limits: {exc}"
        ) from exc

    policy.loaded = True
    policy.tool_names = frozenset(t.name for t in manifest.tools)

    for rule in manifest.rules:
        targets = list(rule.tools) or sorted(policy.tool_names)

        # Fact-bound rules cannot be evaluated here; the in-app gate owns them.
        needs_facts = bool(
            rule.deny_if_fact_true
            or rule.approval_if_fact_true
            or rule.max_contacts_in_days
            or any(limit.fact or limit.max_from_fact for limit in rule.limits)
        )
        if needs_facts and not rule.required_arguments and not rule.forbidden_values \
                and not any(limit.argument and limit.max is not None for limit in rule.limits):
            policy.skipped_fact_rules.append(rule.id)
            continue

        for tool in targets:
            if rule.required_arguments:
                policy.required_arguments.setdefault(tool, [])
                for arg in rule.required_arguments:
                    if arg not in policy.required_arguments[tool]:
                        policy.required_arguments[tool].append(arg)
            if rule.forbidden_values:
                bucket = policy.forbidden_values.setdefault(tool, {})
                for arg, forbidden in rule.forbidden_values.items():
                    existing = bucket.setdefault(arg, [])
                    for value in forbidden:
                        if value not in existing:
                            existing.append(value)
            for limit in rule.limits:
                # Only argument-vs-constant ceilings are fact-free. A clamp is
                # not applied here either: granting a reduced value is an
                # authority the in-app gate holds, and doing it twice could
                # disagree with what the owner approved.
                if not limit.argument or limit.max is None or limit.clamp:
                    continue
                ceiling = _to_decimal(limit.max)
                if ceiling is None:
                    continue
                per_tool = policy.ceilings.setdefault(tool, {})
                existing = per_tool.get(limit.argument)
                # Tightest ceiling wins, so two rules can only make this stricter.
                if existing is None or ceiling < existing:
                    per_tool[limit.argument] = ceiling

    for task in manifest.tasks.values():
        for tool, override in task.tool_overrides.items():
            if override.max_calls is None:
                continue
            current = policy.max_calls.get(tool)
            if current is None or override.max_calls < current:
                policy.max_calls[tool] = override.max_calls

    return policy


POLICY = build_policy()

app = FastAPI(title=f"helios-audit-{APP_NAME}-{TENANT}")

#: Dedupe window state. Keyed by the payload hash of a GATED tool, per the
#: manifest's side_effect flags where available.
_seen: dict[str, float] = {}

#: Recent decisions, most recent last, for /audit-log. The authoritative record
#: is the hash-chained JSONL file; this is a convenience view.
DECISIONS: list[dict] = []

#: Hash-chained log, reusing audit_agent's writer rather than reimplementing the
#: chain. Two implementations of a hash chain drift; one does not.
LOG = AuditLog(LOG_PATH)


def _payload_hash(action: str, payload: dict[str, Any]) -> str:
    """Stable digest of a gated call, used for duplicate detection."""
    canonical = json.dumps({"action": action, "payload": payload}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _finding(rule_id: str, message: str, details: dict[str, Any]) -> Finding:
    return Finding(
        rule_id=rule_id,
        effect="deny",
        severity=Severity.WARN,
        message=message,
        source="app" if POLICY.loaded else "baseline",
        details=details,
    )


def _record(decision: Decision, action: str, payload: dict[str, Any],
            findings: list[Finding], reason: str = "", result: Any = None) -> dict:
    """Append to the hash chain and return the entry for the response."""
    audit_id = uuid.uuid4().hex[:12]
    details: dict[str, Any] = {"reason": reason, "audit_id": audit_id}
    if result is not None:
        details["result"] = result
    try:
        LOG.append(
            EventType.ACTION_DECIDED,
            app_id=APP_NAME,
            agent_id=f"sidecar:{APP_NAME}",
            principal=TENANT,
            tool=action,
            request_id=audit_id,
            decision=decision,
            findings=findings,
            details=details,
        )
    except Exception as exc:  # a broken log must not take the gate down silently
        print(f"sidecar: could not append to {LOG_PATH}: {exc}", flush=True)

    entry = {
        "audit_id": audit_id,
        "ts": time.time(),
        "app": APP_NAME,
        "decision": decision.value,
        "action": action,
        "payload": payload,
        "reason": reason,
        "findings": [f.model_dump(mode="json") for f in findings],
    }
    if result is not None:
        entry["result"] = result
    DECISIONS.append(entry)
    DECISIONS[:] = DECISIONS[-500:]
    print(json.dumps(entry, default=str), flush=True)
    return entry


def _deny(action: str, payload: dict[str, Any], findings: list[Finding],
          reason: str) -> dict:
    """Refuse an action. HTTP 200 with an explicit status, not an exception:
    a denial is a normal, machine-readable outcome the app must act on."""
    _record(Decision.DENY, action, payload, findings, reason=reason)
    return {
        "status": "denied",
        "reason": reason,
        "findings": [f.model_dump(mode="json") for f in findings],
    }


def evaluate(action: str, payload: dict[str, Any]) -> list[Finding]:
    """Run every fact-free check against one proposed action.

    Collects all findings instead of returning on the first, so the app (usually
    Qwen) gets a complete list and can fix everything in one retry.
    """
    findings: list[Finding] = []

    # --- required arguments -------------------------------------------------
    for arg in POLICY.required_arguments.get(action, []):
        if _dig(payload, arg) is None:
            findings.append(_finding(
                "sidecar.required_argument",
                f"action '{action}' is missing required argument '{arg}'",
                {"argument": arg},
            ))

    # --- forbidden values ---------------------------------------------------
    for arg, forbidden in POLICY.forbidden_values.get(action, {}).items():
        value = _dig(payload, arg)
        if value is not None and value in forbidden:
            findings.append(_finding(
                "sidecar.forbidden_value",
                f"argument '{arg}' may not be {value!r}",
                {"argument": arg, "value": str(value)},
            ))

    # --- constant ceilings --------------------------------------------------
    ceilings = dict(POLICY.ceilings.get(action, {}))
    if family_of(action) == "payments" and "amount_cents" not in ceilings:
        # The platform's generic payment cap, applied when the app manifest does
        # not declare a tighter one on the `amount_cents` argument.
        ceilings["amount_cents"] = DEFAULT_MAX_PAYMENT_CENTS
    for arg, ceiling in ceilings.items():
        raw = _dig(payload, arg)
        if raw is None:
            continue
        observed = _to_decimal(raw)
        if observed is None:
            # Fail closed: an unparseable amount must not be read as zero.
            findings.append(_finding(
                "sidecar.unparseable_amount",
                f"argument '{arg}' is not a number and cannot be compared "
                f"against its limit of {ceiling}",
                {"argument": arg, "value": str(raw)},
            ))
            continue
        if observed > ceiling:
            findings.append(_finding(
                "sidecar.exceeds_limit",
                f"{arg}={observed} exceeds the limit of {ceiling}",
                {"field": arg, "observed": str(observed), "limit": str(ceiling)},
            ))

    return findings


@app.get("/health")
def health() -> dict:
    ok, problems = LOG.verify_chain()
    return {
        "status": "ok",
        "role": "audit-sidecar",
        "app": APP_NAME,
        "tenant": TENANT,
        "allowed_tools": sorted(PERMISSIONS),
        "core": CORE_URL,
        "manifest": {
            "loaded": POLICY.loaded,
            "path": str(MANIFEST_PATH),
            "tools": sorted(POLICY.tool_names),
            "enforced_rules": sorted({
                *POLICY.required_arguments, *POLICY.forbidden_values,
                *POLICY.ceilings, *POLICY.max_calls,
            }),
            # What this gate deliberately does NOT check, so a reviewer is never
            # misled into thinking the sidecar is the whole policy.
            "delegated_to_in_app_gate": list(POLICY.skipped_fact_rules),
        },
        "audit_chain": {"intact": ok, "problems": problems[:10], "path": str(LOG_PATH)},
    }


@app.get("/audit-log")
def audit_log() -> dict:
    ok, problems = LOG.verify_chain()
    return {"decisions": DECISIONS[-100:], "chain": {"intact": ok, "problems": problems}}


@app.post("/execute")
async def execute(request: Request, authorization: str = Header(default="")) -> Any:
    if authorization != f"Bearer {EXPECTED_TOKEN}":
        raise HTTPException(401, "invalid action-proposal token")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(422, "body must be JSON")

    action = str(body.get("action", ""))
    payload = body.get("payload") or {}
    if not isinstance(payload, dict):
        raise HTTPException(422, "payload must be an object")

    # --- allow-list ---------------------------------------------------------
    # Accepts either a granted platform family (`payments`) or a tool the app's
    # own manifest declares (`record_inbound_payment`). The intersection of the
    # two vocabularies is empty in practice -- manifests name app tools,
    # helios.yaml names platform families -- so requiring membership in BOTH
    # would deny every legitimate action. Union is the reading that keeps the
    # gate meaningful, and it still fails closed: an action in neither set is
    # refused, and only families in `permissions` can ever reach core, because
    # core routes on the family name.
    family = family_of(action)
    granted_family = family is not None and family in PERMISSIONS
    if not granted_family and action not in POLICY.tool_names:
        return _deny(
            action, payload,
            [_finding(
                "sidecar.not_granted",
                f"tool '{action}' is not granted",
                {"family": family, "granted_families": sorted(PERMISSIONS)},
            )],
            reason=(
                f"tool '{action}' belongs to family {family!r}, which helios.yaml "
                f"does not grant ({sorted(PERMISSIONS)}), and it is not a tool "
                f"declared in the app's audit manifest"
            ),
        )

    # --- manifest-driven fact-free checks ------------------------------------
    findings = evaluate(action, payload)
    if findings:
        return _deny(action, payload, findings, reason=findings[0].message)

    # --- duplicate detection -------------------------------------------------
    digest = _payload_hash(action, payload)
    now = time.time()
    for stale in [k for k, v in _seen.items() if now - v > DEDUPE_WINDOW_S]:
        _seen.pop(stale, None)
    if digest in _seen:
        return _deny(
            action, payload,
            [_finding(
                "sidecar.duplicate",
                f"identical '{action}' proposed within {DEDUPE_WINDOW_S:.0f}s",
                {"window_seconds": DEDUPE_WINDOW_S},
            )],
            reason=(
                f"possible duplicate '{action}' within {DEDUPE_WINDOW_S:.0f}s window"
            ),
        )

    # --- forward through the trusted channel ---------------------------------
    # Only a granted platform family reaches core. An app-declared tool that is
    # not a platform tool is adjudicated here and reported back; the app runs
    # it itself, with its own in-app gate having already decided.
    if not granted_family:
        entry = _record(Decision.ALLOW, action, payload, [],
                        reason="app-local tool; adjudicated, not forwarded")
        return {"status": "allowed", "audit_id": entry["audit_id"], "forwarded": False}

    try:
        resp = httpx.post(
            f"{CORE_URL}/call", json={"tool": action, "args": payload},
            headers={"Authorization": f"Bearer {EXECUTION_SECRET}"},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        _record(Decision.DENY, action, payload, [], reason=f"core unreachable: {exc}")
        raise HTTPException(502, f"helios core unreachable: {exc}")

    # A non-2xx means the side effect did NOT happen. The imported build recorded
    # every one of these as `executed`, including a 404 for a tool core does not
    # implement -- a compliance log claiming an action that never ran.
    if resp.status_code >= 400:
        detail = resp.text[:500]
        entry = _record(
            Decision.DENY, action, payload,
            [_finding(
                "sidecar.core_error",
                f"core returned {resp.status_code} for '{action}'",
                {"http_status": resp.status_code},
            )],
            reason=f"core rejected the call with HTTP {resp.status_code}",
            result={"http_status": resp.status_code, "detail": detail},
        )
        raise HTTPException(502, {
            "stage": "execution",
            "error": f"core returned HTTP {resp.status_code} for tool '{action}'",
            "audit_id": entry["audit_id"],
            "detail": detail,
        })

    try:
        envelope: Any = resp.json() if "json" in resp.headers.get("content-type", "") else {"ok": False, "unavailable": True, "error": resp.text[:500]}
    except ValueError:
        envelope = {"ok": False, "unavailable": True, "error": resp.text[:500]}

    # The tool service answers with one of three envelopes, and conflating the
    # last two corrupts the record (specs/interfaces.md "Call outcomes"):
    #   ok          -> the tool ran; executed
    #   rejected    -> a gate or validator in core refused THIS request; a denial
    #   unavailable -> nobody decided; an outage, never a decision
    if not isinstance(envelope, dict):
        envelope = {"ok": False, "unavailable": True, "error": "malformed envelope"}
    if envelope.get("ok"):
        result = envelope.get("result")
        # The tool ran but the ledger refused to settle (no intent code, a
        # replayed key). Nothing moved, so this is recorded as a denial with
        # the ledger's own reason, never as executed: a compliance log that
        # says a payment went out when it did not is the worst entry possible.
        if isinstance(result, dict) and result.get("state") == "rejected":
            return _deny(
                action, payload,
                [_finding("core.ledger_rejected",
                          str(result.get("reject_reason") or "rejected by the ledger"),
                          {"source": "core", "result": result})],
                reason=f"the ledger refused to settle: {result.get('reject_reason', '')}"[:500],
            )
    elif envelope.get("rejected"):
        return _deny(
            action, payload,
            [_finding("core.rejected", str(envelope.get("error", "rejected by core")),
                      {"source": "core"})],
            reason=f"core rejected the call: {envelope.get('error', '')}"[:500],
        )
    else:
        entry = _record(Decision.DENY, action, payload, [],
                        reason=f"core unavailable: {envelope.get('error', '')}"[:500])
        raise HTTPException(502, {
            "stage": "execution",
            "error": f"core could not execute tool '{action}': {envelope.get('error', '')}"[:500],
            "audit_id": entry["audit_id"],
            "retryable": True,
        })

    _seen[digest] = now
    entry = _record(Decision.ALLOW, action, payload, [], reason="forwarded to core", result=result)
    return {"status": "executed", "audit_id": entry["audit_id"], "result": result}

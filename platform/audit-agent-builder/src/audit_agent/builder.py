"""The builder: manifest in, running audit agent out.

This is the module Qwen actually uses. It answers "how do I give this app an
audit agent" with one call, and it keeps every construction decision in one
place so a generated app cannot end up with an auditor that was quietly
configured to be permissive.

`build_audit_agent(manifest)` does five things, in this order:

1. Builds the tool registry from the manifest's declarations.
2. Composes policy — platform baseline plus the app pack — which is where
   any attempt to weaken the baseline is refused.
3. Wires a fact source. With none supplied, facts are empty and rules that
   need them fail closed; that is safe, but it is also a configuration
   mistake, so it is reported by `describe()`.
4. Opens the append-only log.
5. Signs. A missing key is an error unless local development explicitly
   opted into unsigned grants.

The result is a normal `AuditAgent`. Nothing about it is app-specific in
code, which is what makes the same runtime packageable into every generated
application.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .advisory import (
    AdvisoryReviewer,
    CombinedReviewer,
    HeuristicReviewer,
    NoOpReviewer,
    QwenAdvisoryReviewer,
)
from .agent import AuditAgent
from .audit_log import AuditLog
from .errors import PolicyError, SigningKeyMissingError
from .facts import FactSource
from .manifest import AuditManifest
from .policy import BASELINE_RULE_IDS, Keyring, PolicyEngine, ToolRegistry


def build_reviewer(manifest: AuditManifest) -> AdvisoryReviewer:
    """Assemble the advisory reviewers the manifest asked for.

    Advisory output can never change a verdict, so enabling `qwen` without
    an API key degrades to heuristic-only commentary rather than failing.
    """
    wanted = manifest.settings.advisory
    if not wanted or wanted == ["none"]:
        return NoOpReviewer()
    reviewers: list[AdvisoryReviewer] = []
    if "heuristic" in wanted:
        reviewers.append(HeuristicReviewer())
    if "qwen" in wanted:
        reviewers.append(
            QwenAdvisoryReviewer(model=manifest.settings.advisory_model)
        )
    if not reviewers:
        return NoOpReviewer()
    return reviewers[0] if len(reviewers) == 1 else CombinedReviewer(reviewers)


def build_audit_agent(
    manifest: AuditManifest | str | Path,
    *,
    fact_source: FactSource | None = None,
    keyring: Keyring | None = None,
    log_path: str | Path | None = None,
    reviewer: AdvisoryReviewer | None = None,
    data_dir: str | Path | None = None,
    now: Any = None,
) -> AuditAgent:
    """Construct a running audit agent from a manifest.

    Parameters beyond the manifest are injection points for deployment and
    tests; every one has a safe default.

    `data_dir` resolves a relative `settings.log_path` against the app's own
    data directory, so a generated app does not have to hard-code where its
    evidence lives.
    """
    if isinstance(manifest, (str, Path)):
        manifest = AuditManifest.load(manifest)

    registry: ToolRegistry = manifest.tool_registry()
    pack = manifest.policy_pack()

    if keyring is None:
        keyring = Keyring.from_env(
            allow_unsigned=manifest.settings.allow_unsigned_grants
        )
    if not keyring.can_sign and not keyring.allow_unsigned:
        raise SigningKeyMissingError(
            f"cannot build the audit agent for '{manifest.app_id}': no signing "
            f"key is set in {Keyring.ENV_VAR}. Set it, or set "
            "settings.allow_unsigned_grants=true for local development only."
        )

    target = Path(log_path if log_path is not None else manifest.settings.log_path)
    if not target.is_absolute():
        base = Path(data_dir) if data_dir else Path.cwd()
        target = base / target

    log = AuditLog(target)
    agent = AuditAgent.build(
        registry,
        app_packs=[pack],
        fact_source=fact_source,
        log_path=target,
        keyring=keyring,
        reviewer=reviewer or build_reviewer(manifest),
        default_ttl_seconds=manifest.settings.default_ttl_seconds,
        contact_window_days=manifest.settings.contact_window_days,
        now=now,
    )
    # Attach the manifest identity so logs, grants and reports can name the
    # app the auditor belongs to.
    agent.manifest = manifest
    return agent


def describe(manifest: AuditManifest, agent: AuditAgent | None = None) -> dict[str, Any]:
    """A configuration report, for a build pipeline or an owner.

    Surfaces the things that are valid but worth knowing: whether a fact
    source is wired (without one, fact-bound limits refuse everything),
    whether grants are signed, and what authority each task carries.
    """
    settings = manifest.settings
    report: dict[str, Any] = {
        "app_id": manifest.app_id,
        "name": manifest.name,
        "tasks": sorted(manifest.tasks),
        "tools": {
            t.name: "gated" if t.side_effect else "read-only" for t in manifest.tools
        },
        "app_rules": [r.id for r in manifest.rules],
        "baseline_rules": sorted(BASELINE_RULE_IDS),
        "advisory": list(settings.advisory),
        "log_path": settings.log_path,
        "warnings": [],
    }
    if agent is not None:
        report["signed_grants"] = agent.keyring.can_sign
        report["fact_source_wired"] = agent.facts.source is not None
        if not report["fact_source_wired"]:
            report["warnings"].append(
                "no fact source is wired, so any limit bound to a trusted fact "
                "(amounts, contact counts, changed payment details) will refuse "
                "rather than evaluate"
            )
        if not agent.keyring.can_sign:
            report["warnings"].append(
                "grants are unsigned; only an auditor built with "
                "allow_unsigned_grants will accept them"
            )
    return report


__all__ = ["build_audit_agent", "build_reviewer", "describe"]

"""The `agent-toolkit` command line.

    agent-toolkit inspect <action-manifest>
    agent-toolkit validate <action-manifest> [--audit <audit-manifest>]
    agent-toolkit demo [--run-dir DIR]

`pyproject.toml` has declared `agent-toolkit = "agent_toolkit.cli:main"` since the
package was created, but this module did not exist, so the installed console script
crashed with `ModuleNotFoundError` on every invocation. That is worse than the
script not existing at all: a broken entry point looks like a broken install.

Exit codes: 0 clean, 1 problems found, 2 usage error — the same convention as
`entry-point/checks/check_app.py` and `helios-app`, so a pipeline can treat all
three alike.

`validate` is the cross-check that matters most. The action manifest says what the
agent will try to do; the audit manifest says what may be granted at all. Those two
files are written by different people at different times, and the failure mode when
they disagree is an ungated side effect — so `validate --audit` compares them
rather than trusting either one alone.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Sequence

__all__ = ["main", "build_parser"]

#: Fixed so `demo` is reproducible: the fixtures use relative dates resolved
#: against "today", and a demo whose verdicts change with the calendar is a demo
#: nobody can write a test against.
DEMO_TODAY = date(2026, 10, 15)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-toolkit",
        description="Inspect, validate and demo an action agent's manifest.",
        epilog="Run `agent-toolkit <command> --help` for that command's options.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    i = sub.add_parser(
        "inspect",
        help="print a manifest's tools, mandate and max_turns",
        description="Print what an action manifest declares: its tools, its "
                    "mandate (allowed tools and limits) and its turn budget.")
    i.add_argument("manifest", help="path to the action manifest JSON")
    i.add_argument("--json", action="store_true", dest="as_json",
                   help="emit machine-readable JSON instead of prose")

    v = sub.add_parser(
        "validate",
        help="check a manifest loads, and agrees with the audit manifest",
        description="Load the action manifest under its own validator. With "
                    "--audit, also load the audit manifest and check the "
                    "cross-file invariants between them.")
    v.add_argument("manifest", help="path to the action manifest JSON")
    v.add_argument("--audit", default=None,
                   help="path to the audit manifest YAML; enables the cross-file checks")
    v.add_argument("--quiet", action="store_true",
                   help="print only problems")

    d = sub.add_parser(
        "demo",
        help="run a scripted call sequence through the gate, with no model",
        description="Drive the store, the audit gate and the action log through a "
                    "fixed sequence of proposals: one allowed, one held, one "
                    "denied, one undeclared. No model is called and no network is "
                    "used, so the output is identical every run.")
    d.add_argument("--run-dir", default=None,
                   help="where to write the run log (default: a temporary directory)")
    d.add_argument("--json", action="store_true", dest="as_json",
                   help="emit machine-readable JSON instead of prose")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {"inspect": _inspect, "validate": _validate, "demo": _demo}[args.command]
    try:
        return handler(args)
    except _UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


class _UsageError(RuntimeError):
    """A mistake in how the command was invoked, as opposed to a finding."""


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------

def _inspect(args: argparse.Namespace) -> int:
    from .manifest import ManifestError, load_manifest

    try:
        manifest = load_manifest(args.manifest)
    except (ManifestError, OSError, json.JSONDecodeError) as exc:
        raise _UsageError(f"{args.manifest} could not be loaded: {_one_line(exc)}") from exc

    if args.as_json:
        print(json.dumps({
            "name": manifest.name,
            "version": manifest.version,
            "model": manifest.model,
            "max_turns": manifest.max_turns,
            "tools": list(manifest.tools),
            "mandate": {
                "allowed_tools": list(manifest.mandate.allowed_tools),
                "limits": manifest.mandate.limits.to_dict(),
            },
        }, indent=2))
        return 0

    limits = manifest.mandate.limits.to_dict()
    print(f"{args.manifest}")
    print(f"  name:       {manifest.name}")
    print(f"  version:    {manifest.version}")
    print(f"  model:      {manifest.model or '(default)'}")
    print(f"  max_turns:  {manifest.max_turns}")
    print(f"  tools ({len(manifest.tools)}):")
    for tool in manifest.tools:
        allowed = tool in manifest.mandate.allowed_tools
        print(f"    {'+' if allowed else '-'} {tool}"
              f"{'' if allowed else '   (not in mandate.allowed_tools)'}")
    print(f"  mandate.allowed_tools: {list(manifest.mandate.allowed_tools)}")
    print(f"  mandate.limits: {limits if limits else '{} (no limits set)'}")
    if not limits:
        # Worth saying out loud: an empty limits block means the only policy is
        # the allowed_tools list, which cannot express a ceiling.
        print("    note: no numeric limits. The only bound on this agent is which "
              "tools it may call, not how large an action may be.")
    return 0


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def _validate(args: argparse.Namespace) -> int:
    from .manifest import ManifestError, load_manifest

    problems: list[str] = []
    notes: list[str] = []

    try:
        action = load_manifest(args.manifest)
        notes.append(f"action manifest loads: {action.name}")
    except (ManifestError, OSError, json.JSONDecodeError) as exc:
        print(f"FAIL  {args.manifest}: {_one_line(exc)}")
        print(f"\n1 error(s), 0 warning(s)")
        return 1

    if not args.audit:
        _emit(problems, notes, args.quiet)
        return 1 if problems else 0

    # Imported here rather than at module scope: this package must not depend on
    # the audit builder. `agent-integration` is the only package allowed to import
    # both, and a CLI that hard-imported audit_agent would quietly break that
    # boundary for anyone who installed only the action builder.
    try:
        from audit_agent.manifest import AuditManifest
    except ImportError:
        print("FAIL  --audit was given but audit_agent is not installed; install "
              "audit-agent-builder, or validate the action manifest on its own")
        print(f"\n1 error(s), 0 warning(s)")
        return 1

    try:
        audit = AuditManifest.load(args.audit)
        notes.append(f"audit manifest loads: app_id={audit.app_id}")
    except Exception as exc:
        problems.append(f"{args.audit}: invalid audit manifest — "
                        f"{type(exc).__name__}: {_one_line(exc)}")
        _emit(problems, notes, args.quiet)
        return 1

    problems.extend(_cross_file_problems(action, audit, args))
    _emit(problems, notes, args.quiet)
    return 1 if problems else 0


def _cross_file_problems(action: Any, audit: Any, args: argparse.Namespace) -> list[str]:
    """The invariants between the two manifests.

    Checked in the order they matter. The first is the one that protects money:
    the audit agent is the registry of what may be granted at all, so a tool the
    action agent offers and the auditor has never declared is not denied — it is
    never looked at, and runs with no gate.
    """
    problems: list[str] = []
    declared = {t.name: t for t in audit.tools}
    offered = set(action.tools) | set(action.mandate.allowed_tools)

    # 1. offered ⊆ declared
    undeclared = sorted(offered - set(declared))
    if undeclared:
        problems.append(
            f"action manifest offers tool(s) the audit manifest has not declared: "
            f"{undeclared}. An undeclared tool is not denied — it is never looked "
            f"at, so it runs with no gate. Declare each one under `tools:` in "
            f"{args.audit}.")

    # 2. declared ⊆ offered (the reverse direction)
    unusable = sorted(set(declared) - offered)
    if unusable:
        problems.append(
            f"audit manifest declares tool(s) the action manifest does not offer: "
            f"{unusable}. A tool nobody can call is dead policy; either add it to "
            "the action manifest's `tools` or remove it from the audit manifest.")

    # 3. a side-effecting tool must actually be grantable
    for name in sorted(offered & set(declared)):
        tool = declared[name]
        if name not in action.tools:
            continue
        # The action manifest carries no side_effect flag of its own, so comparing
        # flags belongs to `helios-app validate`, which can import the app's
        # tools.py. With only the two manifests in hand, what is checkable is
        # whether a gated tool is grantable at all.
        if tool.side_effect and name not in action.mandate.allowed_tools:
            problems.append(
                f"tool {name!r} is side-effecting in the audit manifest but is not "
                f"in mandate.allowed_tools, so the agent can never be granted it. "
                "Either add it to the mandate or mark it read-only.")

    # 4. identity agreement
    if action.name != audit.app_id:
        problems.append(
            f"action manifest name {action.name!r} != audit manifest app_id "
            f"{audit.app_id!r}; two files that look like one app are two, and "
            "grants and audit-log entries would not line up.")

    # 5. every tool the agent offers must be allowed by at least one task,
    #    otherwise authorize() denies everything and the app looks broken.
    task_tools = set()
    for task in audit.tasks.values():
        task_tools |= set(task.allowed_tools)
    blocked = sorted(offered - task_tools)
    if blocked:
        problems.append(
            f"no audit task allows tool(s) {blocked}, so the agent is denied them "
            "at authorize() time regardless of the mandate. Add them to a task's "
            "`allowed_tools`.")

    return problems


def _emit(problems: list[str], notes: list[str], quiet: bool) -> None:
    if not quiet:
        for note in notes:
            print(f"  ok    {note}")
    for problem in problems:
        print(f"  FAIL  {problem}")
    print(f"\n{len(problems)} error(s), 0 warning(s)")


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------

def _demo(args: argparse.Namespace) -> int:
    """A scripted run through the gate, with no model and no network.

    Four proposals, one per outcome, so the demo shows the whole decision space
    rather than only the happy path:

      * allowed   — within the mandate, executes
      * held      — over the owner-approval threshold, saved and resumable
      * denied    — contacted too recently, refused with the reason
      * undeclared— not registered at all, refused by the gate

    The point of the last two is that they never reach a human. A demo that only
    showed approvals would not demonstrate that the control works when the agent
    misbehaves, which is the only case the control exists for.
    """
    from .action_log import ActionLog
    from .audit import AuditGate
    from .contracts import ToolRegistry
    from .holds import HoldStore
    from .models import APPROVE, HOLD, Mandate, MandateLimits, Proposal, REJECT
    from .store import Store
    from .tools import build_mock_tools

    store = Store.from_fixtures(today=DEMO_TODAY)
    registry = ToolRegistry(build_mock_tools(store))

    mandate = Mandate(
        allowed_tools=("lookup_invoice", "send_email"),
        limits=MandateLimits(
            max_extension_days=14,
            extension_owner_approval_days=7,
            min_days_between_contacts=3,
            invoice_owner_approval_amount=10000.0,
        ),
    )
    gate = AuditGate(registry, store)

    run_dir = Path(args.run_dir) if args.run_dir else _temp_run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    log = ActionLog(run_dir, run_id="demo-run-1", agent="demo-agent")
    holds = HoldStore(run_dir)

    log.append("run_started", manifest="demo", model=None)

    # (label, tool, arguments, expected decision, why it is in the demo)
    script: list[tuple[str, str, dict[str, Any], str, str]] = [
        ("allowed", "send_email",
         {"customer_id": "CUST-001", "invoice_id": "INV-1001",
          "subject": "Invoice INV-1001 is overdue",
          "body": "A reminder about the outstanding balance."},
         APPROVE,
         "INV-1001 is 4,200 and CUST-001 was last contacted 21 days ago, so this "
         "is inside every limit"),
        ("held", "send_email",
         {"customer_id": "CUST-003", "invoice_id": "INV-1003",
          "subject": "Invoice INV-1003 is overdue",
          "body": "A reminder about the outstanding balance."},
         HOLD,
         "INV-1003 is 12,500, at or above the 10,000 owner-approval threshold"),
        ("denied", "send_email",
         {"customer_id": "CUST-002", "invoice_id": "INV-1002",
          "subject": "Invoice INV-1002 is overdue",
          "body": "A reminder about the outstanding balance."},
         REJECT,
         "CUST-002 was contacted 1 day ago and the mandate requires 3 between "
         "contacts"),
        ("undeclared", "delete_everything", {}, REJECT,
         "no such tool exists, so the gate refuses it rather than running it"),
    ]

    results: list[dict[str, Any]] = []
    for label, tool, arguments, expected, why in script:
        proposal = Proposal.create(agent="demo-agent", tool=tool, arguments=arguments)
        log.append("tool_call", tool=tool, arguments=dict(arguments),
                   side_effect=_side_effect(registry, tool))
        log.append("proposal", proposal_id=proposal.proposal_id, tool=tool,
                   arguments=dict(arguments))

        verdict = gate.check(proposal, mandate)
        log.append("verdict", proposal_id=proposal.proposal_id,
                   decision=verdict.decision, rule=verdict.rule,
                   reason=verdict.reason)

        executed = False
        hold_id = None
        if verdict.approved:
            result = _execute(registry, proposal)
            executed = result.get("executed", False)
            log.append("action_result", tool=tool, arguments=dict(arguments),
                       executed=executed, result=result.get("result"),
                       proposal_id=proposal.proposal_id)
        elif verdict.decision == HOLD:
            record = _hold_record(proposal, verdict)
            path = holds.save(record)
            hold_id = proposal.proposal_id
            log.append("hold", proposal_id=proposal.proposal_id, rule=verdict.rule,
                       reason=verdict.reason, path=str(path))
        else:
            log.append("action_result", tool=tool, arguments=dict(arguments),
                       executed=False, reason=verdict.reason,
                       proposal_id=proposal.proposal_id)

        if verdict.decision != expected:
            # The demo asserts its own expectations. A gate that quietly changed
            # behaviour would otherwise still print a tidy run.
            print(f"  UNEXPECTED  {label}: expected {expected}, got "
                  f"{verdict.decision} ({verdict.rule})", file=sys.stderr)

        results.append({
            "label": label, "tool": tool, "decision": verdict.decision,
            "rule": verdict.rule, "reason": verdict.reason, "executed": executed,
            "hold_id": hold_id, "expected": expected, "why": why,
        })

    # The held proposal is saved and resumable; prove it round-trips rather than
    # merely asserting that a file was written.
    held = [r for r in results if r["decision"] == HOLD]
    resumed = None
    if held:
        record = holds.load("demo-run-1", held[0]["hold_id"])
        resumed = {"proposal_id": record.proposal.proposal_id,
                   "tool": record.proposal.tool,
                   "arguments": dict(record.proposal.arguments),
                   "reason": record.reason}
        log.append("resumed", proposal_id=record.proposal.proposal_id)

    entries = log.entries()
    log.append("run_finished", status="completed", detail=f"{len(entries)} entries")

    if args.as_json:
        print(json.dumps({"run_dir": str(run_dir), "results": results,
                          "resumed": resumed, "log_entries": len(entries) + 1},
                         indent=2, default=str))
        return 0

    print(f"agent-toolkit demo — run dir {run_dir}")
    print(f"mandate: allowed={list(mandate.allowed_tools)} "
          f"limits={mandate.limits.to_dict()}")
    print(f"store today={store.today.isoformat()} (fixed, so the verdicts are "
          "reproducible)\n")
    for r in results:
        marker = {"approve": "ALLOW", "hold": "HOLD ", "reject": "DENY "}[r["decision"]]
        print(f"  [{marker}] {r['label']:<11} {r['tool']}")
        print(f"           rule:   {r['rule']}")
        print(f"           reason: {r['reason']}")
        print(f"           why:    {r['why']}")
        if r["executed"]:
            print("           -> executed; recorded in the store")
        if r["hold_id"]:
            print(f"           -> held as {r['hold_id']}; nothing executed")
        print()

    # The outbox is the observable proof: one email was sent, and it is the one
    # that was allowed. The held and denied proposals reached no customer, and
    # that is the whole claim the demo exists to make.
    sent = store.sent_emails
    print(f"  the store's outbox holds {len(sent)} email(s):")
    for email in sent:
        print(f"    to customer {email.get('customer_id')} re invoice "
              f"{email.get('invoice_id')}")
    if len(sent) != 1:
        print("  UNEXPECTED: exactly one proposal should have executed",
              file=sys.stderr)
    print()

    if resumed:
        print("  the held proposal round-trips through the hold store:")
        print(f"    {resumed['proposal_id']} {resumed['tool']} "
              f"{resumed['arguments']}")
        print()

    kinds = {}
    for entry in entries:
        kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
    print("  action log:")
    for kind in sorted(kinds):
        print(f"    {kind:<16} {kinds[kind]}")
    print(f"    {'TOTAL':<16} {len(entries)}")
    print(f"\n  written to {run_dir}")
    print("  The log is append-only: every entry above was written before the next")
    print("  decision was made, so the record cannot be tidied up afterwards.")
    return 0


def _side_effect(registry: Any, tool: str) -> bool:
    definition = registry.get(tool)
    return bool(definition.side_effect) if definition else False


def _execute(registry: Any, proposal: Any) -> dict[str, Any]:
    definition = registry.get(proposal.tool)
    if definition is None:
        return {"executed": False, "error": f"unknown tool {proposal.tool!r}"}
    try:
        result = definition.handler(proposal.arguments)
    except Exception as exc:  # a handler failure is a result, not a crash
        return {"executed": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"executed": True, "result": result}


def _hold_record(proposal: Any, verdict: Any) -> Any:
    from .holds import HoldRecord
    return HoldRecord(
        run_id="demo-run-1",
        agent=proposal.agent,
        proposal=proposal,
        reason=verdict.reason,
        messages=[{"role": "user", "content": "demo scripted call"}],
        remaining_calls=[],
        # DEMO_TODAY rather than date.today(): the whole point of fixing the
        # store's date is that the verdicts are reproducible, and a hold record
        # stamped with the real date would make the saved artifact differ between
        # runs for no reason.
        created_at=DEMO_TODAY.isoformat(),
    )


def _temp_run_dir() -> Path:
    import tempfile
    return Path(tempfile.mkdtemp(prefix="agent-toolkit-demo-"))


def _one_line(text: Any) -> str:
    return " ".join(str(text).split())


if __name__ == "__main__":
    raise SystemExit(main())

"""Command-line interface for the audit agent.

Three things an operator or a build pipeline actually needs:

    audit-agent validate  <manifest.yaml>   is this manifest sound?
    audit-agent inspect   <manifest.yaml>   what authority does it grant?
    audit-agent serve     <manifest.yaml>   run the auditor over HTTP

Plus a scripted end-to-end `demo`, which exercises a manifest without an
action agent present — useful for checking a generated app's auditor during
a build, and for showing the control working without wiring two systems
together.

Exit codes are meaningful so this slots into CI: 0 ok, 1 validation failed,
2 runtime/configuration error.

No secrets are read here beyond the signing key, which comes from the
environment via `Keyring.from_env()` and is never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .builder import build_audit_agent, describe
from .errors import AuditAgentError, SigningKeyMissingError
from .manifest import AuditManifest, validate_manifest_file


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    elif isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


# --- Commands -------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    ok, problems = validate_manifest_file(args.manifest)
    if ok:
        print(f"{args.manifest}: valid audit manifest")
        return 0
    print(f"{args.manifest}: INVALID")
    for problem in problems:
        print(problem)
    return 1


def cmd_inspect(args: argparse.Namespace) -> int:
    try:
        manifest = AuditManifest.load(args.manifest)
    except AuditAgentError as exc:
        print(f"{args.manifest}: {exc}", file=sys.stderr)
        return 1
    _emit(manifest.summary(), as_json=False)
    if args.json:
        print()
        _emit(
            {
                "app_id": manifest.app_id,
                "tasks": sorted(manifest.tasks),
                "gated_tools": sorted(
                    t.name for t in manifest.tools if t.side_effect
                ),
                "read_only_tools": sorted(
                    t.name for t in manifest.tools if not t.side_effect
                ),
                "app_rules": [r.id for r in manifest.rules],
                "settings": manifest.settings.model_dump(),
            },
            as_json=True,
        )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the audit agent as an HTTP service."""
    from .transport import HttpTransportServer

    try:
        manifest = AuditManifest.load(args.manifest)
        agent = build_audit_agent(
            manifest,
            log_path=args.log_path,
            data_dir=args.data_dir,
        )
    except AuditAgentError as exc:
        print(f"cannot start audit agent: {exc}", file=sys.stderr)
        return 2

    server = HttpTransportServer(agent, host=args.host, port=args.port)
    server.start()
    report = describe(manifest, agent)
    # Flushed explicitly: stdout is block-buffered when redirected or run in
    # a container, and a server that prints its own address only on exit is
    # useless to whoever is waiting to call it.
    print(f"audit agent for '{manifest.app_id}' listening on {server.base_url}", flush=True)
    print(f"  tasks: {', '.join(report['tasks']) or '(none)'}", flush=True)
    print(f"  signed grants: {report['signed_grants']}", flush=True)
    for warning in report["warnings"]:
        print(f"  WARNING: {warning}", file=sys.stderr, flush=True)
    print("  press Ctrl-C to stop", flush=True)
    try:
        # serve_forever already runs on a thread; block the main thread so
        # signals are handled where the operator expects them.
        import threading

        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.stop()
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Run a scripted request sequence against a manifest's audit agent.

    Uses an in-memory fact source built from `--fact` arguments, so the demo
    is reproducible and needs no database. Each scenario prints the verdict
    and the reason, which is the part worth showing: an audit control that
    cannot explain itself is not much use to an owner.
    """
    from decimal import Decimal

    from .contracts import ActionRequest, AuthorizationRequest
    from .facts import StaticFactSource
    from .policy import Keyring

    try:
        manifest = AuditManifest.load(args.manifest)
    except AuditAgentError as exc:
        print(f"{args.manifest}: {exc}", file=sys.stderr)
        return 1

    amounts: dict[str, Decimal] = {}
    contacts: dict[str, int] = {}
    counterparties: dict[str, str] = {}
    changed: dict[str, bool] = {}
    extras: dict[str, dict[str, Any]] = {}
    for spec in args.fact or []:
        # --fact INV-1001:amount_due=450.00
        try:
            subject, pair = spec.split(":", 1)
            key, value = pair.split("=", 1)
        except ValueError:
            print(
                f"--fact must look like SUBJECT:key=value (got '{spec}')",
                file=sys.stderr,
            )
            return 2
        # Route each key to the fact slot the rules actually read. A key
        # routed to the wrong slot would silently leave a limit unevaluable,
        # and the demo would then fail closed for a reason nobody can see.
        if key in ("amount_due", "invoice_amount"):
            try:
                amounts[subject] = Decimal(value)
            except Exception:
                print(f"--fact '{spec}': not a decimal amount", file=sys.stderr)
                return 2
        elif key in ("recent_contact_count", "contact_count"):
            try:
                contacts[subject] = int(value)
            except ValueError:
                print(f"--fact '{spec}': not an integer count", file=sys.stderr)
                return 2
        elif key in ("payment_details_changed", "bank_details_changed", "duplicate_invoice"):
            lowered = value.strip().lower()
            if lowered not in ("true", "false", "1", "0", "yes", "no"):
                print(f"--fact '{spec}': not a boolean", file=sys.stderr)
                return 2
            flag = lowered in ("true", "1", "yes")
            # Only payee-detail flags feed the fraud signal. A duplicate flag
            # is a separate fact; writing it into `changed` would trigger the
            # wrong rule and deny a payment for a reason nobody declared.
            if key in ("payment_details_changed", "bank_details_changed"):
                changed[subject] = flag
            extras.setdefault(subject, {})[key] = flag
        elif key in ("customer", "supplier", "counterparty"):
            counterparties[subject] = value
        else:
            extras.setdefault(subject, {})[key] = value
    fact_source = StaticFactSource(
        amounts=amounts,
        contacts=contacts,
        counterparties=counterparties,
        payment_details_changed=changed,
        extras=extras,
    )

    keyring = Keyring.from_env(allow_unsigned=True)
    if not keyring.can_sign:
        print(
            f"note: {Keyring.ENV_VAR} is not set, so demo grants are unsigned",
            file=sys.stderr,
        )

    log_path = args.log_path or (
        Path(args.data_dir or Path.cwd()) / f"demo-{manifest.app_id}-audit.jsonl"
    )
    agent = build_audit_agent(
        manifest, fact_source=fact_source, keyring=keyring, log_path=log_path
    )

    print(manifest.summary())
    print(f"\naudit log: {log_path}\n")

    task_type = args.task or sorted(manifest.tasks)[0]
    task = manifest.tasks[task_type]
    print(f"=== task '{task_type}' ===")
    print(f"tools: {', '.join(task.allowed_tools)}")
    print(f"grant lifetime: {task.ttl_seconds}s\n")

    authorization = agent.authorize(
        AuthorizationRequest(
            request_id="demo-authorize",
            app_id=manifest.app_id,
            agent_id="demo-action-agent",
            task_type=task_type,
            task_summary=args.intent
            or f"Demonstrate the authority model for task '{task_type}'",
            requested_tools=list(task.allowed_tools),
            context={args.subject_key: args.subject} if args.subject else {},
        )
    )
    print(f"authorization: {authorization.decision.value}")
    print(f"  granted tools: {', '.join(authorization.tool_names()) or '(none)'}")
    print(f"  expires: {authorization.expires_at.isoformat()}")
    print(f"  signed: {authorization.signature is not None}")
    for finding in authorization.findings:
        print(f"  [{finding.severity.value}] {finding.rule_id}: {finding.message}")

    for index, raw_call in enumerate(args.call or []):
        # --call tool:{"key":"value"}
        tool, _, raw_arguments = raw_call.partition(":")
        try:
            arguments = json.loads(raw_arguments) if raw_arguments else {}
        except json.JSONDecodeError as exc:
            print(f"  cannot parse arguments for '{tool}': {exc}", file=sys.stderr)
            return 2

        print(f"\ncall '{tool}' {arguments}")

        # A side-effect call needs its own authorization, because a grant is
        # single-use: spending the first grant on the first call is exactly
        # what stops the same authority being replayed. Asking again per call
        # is the real interaction, so the demo does it too. Read-only tools
        # run directly and reuse nothing.
        call_authorization = authorization
        if agent.registry.has(tool) and agent.registry.side_effect(tool):
            call_authorization = agent.authorize(
                AuthorizationRequest(
                    request_id=f"demo-authorize-{index}",
                    app_id=manifest.app_id,
                    agent_id="demo-action-agent",
                    task_type=task_type,
                    task_summary=args.intent
                    or f"Demonstrate the authority model for task '{task_type}'",
                    requested_tools=list(task.allowed_tools),
                    context={args.subject_key: args.subject} if args.subject else {},
                )
            )
            print(f"  authorization: {call_authorization.decision.value}")

        decision = agent.adjudicate(
            ActionRequest(
                request_id=f"demo-call-{index}",
                app_id=manifest.app_id,
                agent_id="demo-action-agent",
                task_type=task_type,
                tool=tool,
                arguments=arguments,
                authorization=call_authorization,
            )
        )
        print(f"  -> {decision.decision.value}: {decision.reason}")
        if decision.granted_arguments:
            print(f"  granted arguments: {decision.granted_arguments}")
        if decision.hold_id:
            print(f"  hold: {decision.hold_id}")
            print(f"  owner question: {decision.owner_question}")
        for finding in decision.findings:
            print(f"  [{finding.severity.value}] {finding.rule_id}: {finding.message}")
        for note in decision.advisory_notes:
            print(f"  [advisory] {note}")

    if agent.log:
        ok, problems = agent.log.verify_chain()
        print(f"\naudit log: {agent.log.sequence} entries, chain intact: {ok}")
        for problem in problems:
            print(f"  PROBLEM: {problem}")
        if not ok:
            return 2
    return 0


# --- Argument parsing ----------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audit-agent",
        description="Helios Audit Agent Builder: validate, inspect and run an audit agent",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_validate = subparsers.add_parser(
        "validate", help="check whether a manifest is a sound audit agent definition"
    )
    p_validate.add_argument("manifest", help="path to the audit manifest YAML")
    p_validate.set_defaults(func=cmd_validate)

    p_inspect = subparsers.add_parser(
        "inspect", help="print the authority a manifest grants, in plain language"
    )
    p_inspect.add_argument("manifest", help="path to the audit manifest YAML")
    p_inspect.add_argument("--json", action="store_true", help="also emit machine-readable detail")
    p_inspect.set_defaults(func=cmd_inspect)

    p_serve = subparsers.add_parser(
        "serve", help="run the audit agent as an HTTP service for an action agent to call"
    )
    p_serve.add_argument("manifest", help="path to the audit manifest YAML")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8790)
    p_serve.add_argument("--log-path", default=None, help="override the manifest's log path")
    p_serve.add_argument("--data-dir", default=None, help="base directory for a relative log path")
    p_serve.set_defaults(func=cmd_serve)

    p_demo = subparsers.add_parser(
        "demo", help="run a scripted sequence against a manifest's audit agent"
    )
    p_demo.add_argument("manifest", help="path to the audit manifest YAML")
    p_demo.add_argument("--task", default=None, help="task type to exercise (default: first)")
    p_demo.add_argument("--intent", default=None, help="plain-language task intent")
    p_demo.add_argument("--subject-key", default="invoice_id", help="argument key naming the subject")
    p_demo.add_argument("--subject", default=None, help="subject id to act on")
    p_demo.add_argument(
        "--fact",
        action="append",
        metavar="SUBJECT:key=value",
        help="trusted fact for the demo, e.g. INV-1001:amount_due=450.00",
    )
    p_demo.add_argument(
        "--call",
        action="append",
        metavar='TOOL:{"arg":"value"}',
        help='a call to adjudicate, e.g. send_reminder_email:{"invoice_id":"INV-1001","message":"hi"}',
    )
    p_demo.add_argument("--log-path", default=None, help="where to write the demo audit log")
    p_demo.add_argument("--data-dir", default=None, help="base directory for a relative log path")
    p_demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SigningKeyMissingError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except AuditAgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:  # piped into head and friends
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

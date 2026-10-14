"""CLI: spin up a gated agent, watch the audit agent decide, own the holds.

    agent-integration doctor               # is the model reachable? run this first
    agent-integration demo                 # offline, all five scenarios
    agent-integration run --task "..."     # GLM-5.3, interactive approvals
    agent-integration run --offline ...    # scripted, no token needed
    agent-integration inspect              # what does the auditor govern
    agent-integration manifest --out X     # generate the audit YAML
    agent-integration selfcheck            # do the two packages still agree

Everything works with no token at all. ``run`` uses a scripted driver when
``--offline`` is given or no token is configured, and GLM-5.3 otherwise.

Exit codes are meaningful for CI: 0 ran, 1 config or connectivity, 2 refused
(the auditor granted no authority), 3 held with nobody to decide, 4 the run
errored.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .model_driver import DEFAULT_BASE_URL, DEFAULT_MODEL
from .runtime import COMPLETED, ERROR, HELD, REFUSED
from .wiring import DEFAULT_MANIFEST_PATH, Integration, build_integration

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_REFUSED = 2
EXIT_HELD = 3
EXIT_ERROR = 4

#: Scenarios for `demo`, each exercising a different control. The invoice and
#: customer ids are the action side's own fixtures, and the contact counts
#: assume they run in order against one shared auditor, so the frequency cap
#: is reached by actually sending rather than by assertion.
DEMO_SCENARIOS: list[dict[str, Any]] = [
    {
        "title": "Read-only lookup is not gated; a normal reminder is approved",
        "task": "Follow up on overdue invoice INV-1001 for Ridgeline Construction",
        "calls": [
            ("lookup_invoice", {"invoice_id": "INV-1001"}),
            (
                "send_email",
                {
                    "customer_id": "CUST-001",
                    "invoice_id": "INV-1001",
                    "subject": "Invoice RL-2026-047 is overdue",
                    "body": "A courteous reminder that this invoice was due 21 days ago.",
                },
            ),
        ],
    },
    {
        "title": "A large invoice is held for the owner (amount is a FACT)",
        "task": "Follow up on overdue invoice INV-1003 for Harbor Logistics",
        "calls": [
            (
                "send_email",
                {
                    "customer_id": "CUST-003",
                    "invoice_id": "INV-1003",
                    "subject": "Invoice HL-2026-078 is overdue",
                    "body": "This invoice is past due; please advise on payment.",
                },
            ),
        ],
    },
    {
        "title": "An over-long extension is held, not silently sent",
        "task": "Offer Ridgeline a 60-day extension on invoice INV-1001",
        "calls": [
            (
                "send_email",
                {
                    "customer_id": "CUST-001",
                    "invoice_id": "INV-1001",
                    "subject": "Extension offer",
                    "body": "We can extend the due date if that helps.",
                    "extension_offer_days": 60,
                },
            ),
        ],
    },
    {
        "title": "A first reminder to Maple Street Dental is approved",
        "task": "Send Maple Street Dental a reminder about invoice INV-1002",
        "calls": [
            (
                "send_email",
                {
                    "customer_id": "CUST-002",
                    "invoice_id": "INV-1002",
                    "subject": "Invoice MS-2026-112 is overdue",
                    "body": "A courteous reminder that this invoice was due recently.",
                },
            ),
        ],
    },
    {
        "title": "The next one is refused: the trusted contact cap is reached",
        "task": "Send Maple Street Dental another reminder about invoice INV-1002",
        "calls": [
            (
                "send_email",
                {
                    "customer_id": "CUST-002",
                    "invoice_id": "INV-1002",
                    "subject": "Another reminder",
                    "body": "Following up again on this invoice.",
                },
            ),
        ],
    },
]


def _print_header(text: str) -> None:
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def _print_result(result: Any) -> None:
    for action in result.actions:
        verdict = action.get("verdict")
        if verdict is None:
            # A read-only call: executed directly, never adjudicated.
            mark = "READ   " if action.get("executed") else "FAILED "
            print(f"  {mark} {action['tool']}  (read-only, not gated)")
        else:
            mark = {
                "approve": "ALLOW  ",
                "hold": "HOLD   ",
                "reject": "DENY   ",
            }.get(verdict["decision"], str(verdict["decision"]).upper())
            print(f"  {mark} {action['tool']}  (rule: {verdict['rule']})")
            print(f"           {verdict['reason']}")
            if action.get("granted_arguments"):
                print(f"           granted arguments: {action['granted_arguments']}")
        print(f"           executed: {action.get('executed')}")
        if not action.get("executed") and action.get("output"):
            print(f"           output: {str(action['output'])[:120]}")
    if result.status == REFUSED:
        print(f"  REFUSED  the auditor granted no authority for this task")
        if result.detail:
            print(f"           {result.detail}")
    elif result.status == ERROR:
        print(f"  ERROR    {result.detail}")


def _owner_decide(
    integration: Integration, hold: dict[str, Any], *, interactive: bool
) -> bool | None:
    """Ask the owner. Returns True/False, or None if nobody could be asked."""
    print(f"  HELD     {hold['tool']}  (hold id: {hold['hold_id']})")
    print(f"           {hold['owner_question']}")
    if not interactive:
        print("           (non-interactive; leaving the hold pending)")
        return None
    if not sys.stdin.isatty():
        print("           (not a terminal; leaving the hold pending)")
        return None
    try:
        answer = input("           approve this exact action? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return answer.strip().lower() in {"y", "yes"}


def _run_one(
    integration: Integration,
    *,
    task: str,
    task_type: str,
    driver: Any,
    interactive: bool,
) -> Any:
    result = integration.runtime.run(driver=driver, task=task, task_type=task_type)
    _print_result(result)
    for hold in result.holds:
        approve = _owner_decide(integration, hold, interactive=interactive)
        if approve is None:
            continue
        # The decision applies to the exact action that was held, so resume it
        # rather than re-proposing: re-adjudicating would hold again, because
        # the fact that triggered the hold has not changed.
        resumed = integration.runtime.resume(
            driver=driver,
            run_id=result.run_id,
            hold_id=hold["hold_id"],
            decision=approve,
            task=task,
            task_type=task_type,
        )
        _print_header("After the owner decision")
        _print_result(resumed)
        if resumed.final_text:
            print(f"  agent said: {resumed.final_text}")
        result = resumed
    return result


def cmd_demo(args: argparse.Namespace) -> int:
    """Run every scenario offline and show what the auditor decided."""
    from .runtime import ScriptedDriver, ToolCall

    integration = _build(args)
    _print_header("Helios agent integration — offline demo")
    print(f"transport: {args.transport}")
    print(f"auditor governs: {json.dumps(_capabilities(integration), indent=2)}")
    if integration.ephemeral_key and not args.allow_unsigned:
        print(
            "\nnote: HELIOS_AUDIT_SIGNING_KEY is not set, so grants are signed "
            "with an ephemeral process key. Set it for anything persistent."
        )

    exit_code = EXIT_OK
    last_log: Path | None = None
    for index, scenario in enumerate(DEMO_SCENARIOS, start=1):
        _print_header(f"{index}. {scenario['title']}")
        print(f"task: {scenario['task']}")
        driver = ScriptedDriver(
            [ToolCall(name, dict(arguments)) for name, arguments in scenario["calls"]],
            final_text="Task finished.",
        )
        result = _run_one(
            integration,
            task=scenario["task"],
            task_type=args.task_type,
            driver=driver,
            interactive=False,
        )
        # Run directories are uuid-named, so "last" has to be tracked rather
        # than sorted out of the filesystem.
        if result.action_log_path:
            last_log = Path(result.action_log_path)
        if result.status == REFUSED:
            exit_code = EXIT_REFUSED

    _print_header("Action log (final scenario)")
    if last_log and last_log.exists():
        print(f"  {last_log}")
        for line in last_log.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            detail = {
                k: v
                for k, v in entry.items()
                if k not in ("seq", "kind", "ts", "run_id", "agent")
            }
            print(
                f"  [{entry.get('seq')}] {entry.get('kind')}: "
                f"{json.dumps(detail, ensure_ascii=False, default=str)[:150]}"
            )

    _print_header("Audit evidence (hash-chained)")
    audit_log = _audit_log_path(args)
    if audit_log.exists():
        lines = audit_log.read_text(encoding="utf-8").splitlines()
        print(f"  {len(lines)} entries in {audit_log}")
        _verify_chain(integration)
    integration.close()
    return exit_code


def _verify_chain(integration: Integration) -> None:
    """Confirm the audit log is untampered — the property worth demoing."""
    log = getattr(integration.audit_agent, "log", None)
    verifier = getattr(log, "verify_chain", None)
    if not callable(verifier):
        return
    ok, problems = verifier()
    if ok:
        print("  chain verification: OK — no entry has been edited or removed")
    else:
        print(f"  chain verification: FAILED — {problems[:3]}")


def _capabilities(integration: Integration) -> dict[str, Any]:
    caps = integration.client.capabilities()
    return {
        "tasks": {
            name: {
                "allowed_tools": task["allowed_tools"],
                "ttl_seconds": task["ttl_seconds"],
            }
            for name, task in caps.get("tasks", {}).items()
        },
        "tools": {
            name: "gated" if meta.get("gated") else "read-only"
            for name, meta in caps.get("tools", {}).items()
        },
    }


def _audit_log_path(args: argparse.Namespace) -> Path:
    return Path(args.data_dir) / "collect-audit-log.jsonl"


def _build(args: argparse.Namespace) -> Integration:
    integration = build_integration(
        app_id=args.app_id,
        agent_id=args.agent_id,
        task_type=args.task_type,
        transport=args.transport,
        runs_dir=args.runs_dir,
        data_dir=args.data_dir,
        allow_unsigned=args.allow_unsigned,
        instructions=args.instructions,
        manifest_path=(None if args.self_declared_manifest else args.manifest),
    )
    if integration.self_declared_manifest:
        print(
            "warning: the manifest was generated from this command's own "
            "arguments, so it authorizes whatever was asked for. Pass "
            "--manifest <reviewed.yaml> for a real constraint.",
            file=sys.stderr,
        )
    return integration


def cmd_run(args: argparse.Namespace) -> int:
    """Run one agent on one task, with the auditor in the loop."""
    from .model_driver import ModelDriver, api_key_present, token_env_var_name
    from .runtime import ScriptedDriver

    integration = _build(args)
    use_model = not args.offline
    if use_model and not api_key_present():
        print(
            "no model token is set, so falling back to the offline scripted "
            "driver. Copy .env.example to .env and set HELIOS_LLM_API_KEY, then "
            "run `agent-integration doctor` to check the connection.",
            file=sys.stderr,
        )
        use_model = False

    if use_model:
        driver: Any = ModelDriver(model=args.model, base_url=args.base_url or None)
        print(f"driver: {driver.model} via {driver.base_url}")
        print(f"token from: {token_env_var_name()}")
    else:
        driver = ScriptedDriver(
            _scripted_from_args(args), final_text="Task finished."
        )
        print("driver: offline script")

    _print_header(f"Task: {args.task}")
    result = _run_one(
        integration,
        task=args.task,
        task_type=args.task_type,
        driver=driver,
        interactive=not args.non_interactive,
    )
    if result.final_text:
        print(f"\nagent said: {result.final_text}")
    print(f"\nstatus: {result.status}")
    print(json.dumps(result.summary(), indent=2, default=str))
    integration.close()

    if result.status == REFUSED:
        return EXIT_REFUSED
    if result.status == ERROR:
        return EXIT_ERROR
    if result.holds and args.non_interactive:
        return EXIT_HELD
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check model connectivity without running an agent.

    The one command to run after filling in ``.env``. It makes a single
    trivial request, prints the model's reply, and reports exactly which
    setting is wrong if it fails. No tools are offered, so it cannot trigger a
    side effect and never touches the auditor.
    """
    from .model_driver import ModelDriver, ModelError, api_key_present, resolve_token, token_env_var_name

    print("model connectivity check")
    print("-" * 60)
    base_url = (args.base_url or os.environ.get("HELIOS_LLM_BASE_URL", "")).rstrip("/")
    driver = ModelDriver(
        model=args.model,
        base_url=base_url or None,
        reasoning_effort="low",  # cheapest setting; this is a ping, not a task
        max_tokens=64,
    )
    print(f"  model          : {driver.model}")
    print(f"  base url       : {driver.base_url}")
    print(f"  token variable : {token_env_var_name()}")

    token = resolve_token()
    if not token:
        print(f"\nFAILED: no token is set.")
        print(
            f"  Copy .env.example to .env and set one of HELIOS_LLM_API_KEY / "
            f"LLM_API_TOKEN, then re-run this command."
        )
        return EXIT_USAGE
    # Show only the shape, never the value.
    print(f"  token          : {token[:2]}...{token[-2:]} ({len(token)} chars)")

    print(f"\nsending one minimal request to {driver.base_url}/chat/completions ...")
    try:
        reply = driver.next_calls(
            task="ping",
            instructions="Reply with exactly the word READY and nothing else.",
            tools=[],
            history=[{"role": "user", "content": "Reply with exactly READY."}],
        )
    except ModelError as exc:
        print(f"\nFAILED: {exc}")
        print("\nWhat to check:")
        print(f"  - Is the token valid for {driver.base_url}?")
        print(
            "  - If your token was issued by a competition gateway, set "
            "HELIOS_LLM_BASE_URL to that gateway's URL."
        )
        print(f"  - Is the model id '{driver.model}' available on that endpoint?")
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - a doctor reports, never crashes
        print(f"\nFAILED unexpectedly: {type(exc).__name__}: {exc}")
        return EXIT_USAGE

    if isinstance(reply, str):
        print(f"\nOK: the model replied -> {reply[:200]!r}")
    else:
        print(f"\nOK, but the model asked for a tool it was not offered: {reply}")
        print("    The endpoint works; this is a prompt quirk, not a failure.")

    if driver.model != DEFAULT_MODEL:
        print(f"\nnote: the default model is {DEFAULT_MODEL}; you overrode it.")
    print(
        "\nReady. Next: agent-integration run --task \"Follow up on overdue "
        "invoice INV-1001\""
    )
    return EXIT_OK


def _scripted_from_args(args: argparse.Namespace) -> list[Any]:
    """Parse repeated --call 'tool:{json}' flags into ToolCalls."""
    from .runtime import ToolCall

    calls = []
    for raw in args.call or []:
        name, _, payload = raw.partition(":")
        if not name:
            raise SystemExit(f"--call must look like 'tool_name:{{...}}', got {raw!r}")
        try:
            arguments = json.loads(payload or "{}")
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--call {raw!r} has invalid JSON arguments: {exc}")
        calls.append(ToolCall(name=name.strip(), arguments=arguments))
    if not calls:
        raise SystemExit(
            "offline mode needs at least one --call 'tool:{json}' "
            "(or drop --offline to use the model)"
        )
    return calls


def cmd_inspect(args: argparse.Namespace) -> int:
    """Show the authority surface the auditor governs."""
    integration = _build(args)
    _print_header(f"Auditor for app '{args.app_id}'")
    print(json.dumps(_capabilities(integration), indent=2))
    _print_header("Generated audit manifest")
    from .manifest_gen import manifest_to_yaml

    print(manifest_to_yaml(integration.manifest))
    integration.close()
    return EXIT_OK


def cmd_manifest(args: argparse.Namespace) -> int:
    """Generate the audit manifest from the action side's real tools.

    Deliberately does not build an auditor: generation is a data step, and
    the output is meant to be read by a human before it is ever loaded.
    """
    from agent_toolkit.store import Store
    from agent_toolkit.tools import build_mock_tools

    from .manifest_gen import build_audit_manifest, manifest_to_yaml, validate_generated

    store = Store.from_fixtures()
    manifest = build_audit_manifest(
        build_mock_tools(store),
        app_id=args.app_id,
        task_type=args.task_type,
        log_path=str(_audit_log_path(args)),
        mandate=_mandate_from_args(args),
        allow_unsigned_grants=args.allow_unsigned,
    )
    text = manifest_to_yaml(manifest)
    ok, problems = validate_generated(manifest)

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    if not ok:
        print("manifest FAILED validation:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_USAGE
    print("manifest validation: OK", file=sys.stderr)
    return EXIT_OK


def _mandate_from_args(args: argparse.Namespace) -> dict[str, Any]:
    mandate: dict[str, Any] = {}
    if args.extension_approval_days is not None:
        mandate["extension_owner_approval_days"] = args.extension_approval_days
    if args.invoice_approval_amount is not None:
        mandate["invoice_owner_approval_amount"] = args.invoice_approval_amount
    if args.max_contacts is not None:
        mandate["max_contacts_in_window"] = args.max_contacts
    return mandate


def cmd_selfcheck(args: argparse.Namespace) -> int:
    """Prove the seam is closed, without a model. Useful in CI."""
    from .adapters import ToolContractView
    from audit_agent import ToolDescriptor

    integration = build_integration(
        app_id=args.app_id,
        agent_id=args.agent_id,
        task_type=args.task_type,
        runs_dir=args.runs_dir,
        data_dir=args.data_dir,
        allow_unsigned=True,
    )
    problems: list[str] = []
    registry = integration.audit_agent.registry

    for tool in integration.tools:
        if not registry.has(tool.name):
            problems.append(
                f"tool '{tool.name}' exists on the action side but the auditor "
                "has not registered it"
            )
            continue
        descriptor = registry.get(tool.name)
        if descriptor.side_effect != tool.side_effect:
            problems.append(
                f"tool '{tool.name}': action side says side_effect="
                f"{tool.side_effect}, auditor says {descriptor.side_effect}. "
                "A gated tool registered as read-only would never be checked."
            )
        if tool.side_effect and not descriptor.input_schema:
            problems.append(
                f"gated tool '{tool.name}' has no input schema on the audit "
                "side, so malformed arguments would reach execution"
            )
        view = ToolContractView(tool)
        adapted = ToolDescriptor.from_contract(view)
        if adapted.side_effect != tool.side_effect:
            problems.append(
                f"adapter failed for '{tool.name}': from_contract() produced "
                f"side_effect={adapted.side_effect}"
            )

    session = integration.bridge.authorize_task(
        args.task_type,
        "self-check: confirm the auditor grants the runtime's own tools",
        [tool.name for tool in integration.tools],
    )
    if not session.authorized:
        problems.append(f"the auditor refused its own tool set: {session.reason}")
    else:
        missing = {t.name for t in integration.tools} - set(session.allowed_tools)
        if missing:
            problems.append(f"granted set is missing tools: {sorted(missing)}")

    integration.close()
    if problems:
        print("SEAM CHECK FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return EXIT_USAGE
    print("seam check: OK")
    print(f"  tools agree on names, side-effect flags and schemas")
    print(f"  auditor grants: {sorted(session.allowed_tools)}")
    print(f"  gated tools are adjudicated per call")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-integration",
        description="Run an action agent with a standalone audit agent gating it",
    )
    parser.add_argument("--app-id", default="helios-collect")
    parser.add_argument("--agent-id", default="collect-reminder")
    parser.add_argument("--task-type", default="collect.reminder")
    parser.add_argument("--transport", default="inprocess",
                        choices=["inprocess", "file", "http"],
                        help="how the action agent reaches the auditor")
    parser.add_argument("--runs-dir", default=".runs")
    parser.add_argument("--data-dir", default=".helios-data")
    parser.add_argument("--allow-unsigned", action="store_true",
                        help="local development only: skip grant signing")
    parser.add_argument("--instructions", default="")
    parser.add_argument(
        "--manifest", default=str(DEFAULT_MANIFEST_PATH),
        help="the reviewed audit manifest that governs this agent "
             "(default: the one checked into manifests/)",
    )
    parser.add_argument(
        "--self-declared-manifest", action="store_true",
        help="generate the manifest from these arguments instead of loading a "
             "reviewed file. Convenient, but the app then authorizes itself, "
             "so it proves nothing about governance.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("demo", help="run every scenario offline and show verdicts").set_defaults(
        func=cmd_demo
    )

    #: Shared model options, so `run` and `doctor` cannot disagree about which
    #: endpoint is being talked to.
    def _add_model_options(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--model", default=None,
            help=f"model id (default: {DEFAULT_MODEL}, or $HELIOS_LLM_MODEL)",
        )
        target.add_argument(
            "--base-url", default=None,
            help="OpenAI-compatible base URL (default: "
                 f"{DEFAULT_BASE_URL}, or $HELIOS_LLM_BASE_URL). Set this when "
                 "your token was issued by a gateway rather than the public API.",
        )

    doctor = sub.add_parser(
        "doctor",
        help="check model connectivity without running an agent",
    )
    _add_model_options(doctor)
    doctor.set_defaults(func=cmd_doctor)

    run = sub.add_parser("run", help="run one agent on one task")
    run.add_argument("--task", required=True,
                     help="plain-language instruction for the agent")
    _add_model_options(run)
    run.add_argument("--offline", action="store_true",
                     help="use a scripted driver instead of the model")
    run.add_argument("--call", action="append", metavar="TOOL:JSON",
                     help="offline only: a call to make, repeatable")
    run.add_argument("--non-interactive", action="store_true",
                     help="leave holds pending instead of prompting")
    run.set_defaults(func=cmd_run)

    sub.add_parser("inspect", help="show the authority surface and manifest").set_defaults(
        func=cmd_inspect
    )

    manifest = sub.add_parser("manifest", help="generate the audit manifest")
    manifest.add_argument("--out", help="write to this path instead of stdout")
    manifest.add_argument("--extension-approval-days", type=int, default=None)
    manifest.add_argument("--invoice-approval-amount", default=None)
    manifest.add_argument("--max-contacts", type=int, default=None)
    manifest.set_defaults(func=cmd_manifest)

    sub.add_parser("selfcheck", help="verify the two packages agree (CI)").set_defaults(
        func=cmd_selfcheck
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

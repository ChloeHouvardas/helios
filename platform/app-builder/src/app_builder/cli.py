"""The `helios-app` command line.

    helios-app questions                          # print the 16-question intake template
    helios-app spec-from-intake intake.yaml       # build a first spec from the answers
    helios-app new <app-dir> --spec <spec.yaml>   # scaffold the application skeleton
    helios-app validate <app-dir> [--strict]      # check the spec against the manifests and code

The order is the order a coding agent follows: answer the questions, edit the spec,
scaffold, write the policy, validate. See `entry-point/BUILD.md`.

Exit codes are the same ones `entry-point/checks/check_app.py` has always used, so
existing pipelines keep working: 0 clean, 1 problems found, 2 usage error.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

__all__ = ["main", "build_parser"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helios-app",
        description="Turn one sentence of intent into a Helios application skeleton.",
        epilog="Run `helios-app <command> --help` for that command's options.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    q = sub.add_parser(
        "questions",
        help="print the 16-question intake as a fillable YAML template",
        description="Print the fixed 16-question intake as a YAML template with "
                    "`answer: null` and `assumption: null` per item.")
    q.add_argument("-o", "--output", default=None,
                   help="write to this file instead of stdout")

    s = sub.add_parser(
        "spec-from-intake",
        help="build a first helios-app.yaml from answered intake questions",
        description="Build a first spec from the answers that can be mapped. "
                    "Everything else is left as a `# TODO` naming the question "
                    "that would fill it.")
    s.add_argument("intake", help="the answered intake YAML")
    s.add_argument("--app-id", default=None,
                   help="kebab-case identity.id; derived from the job answer if omitted")
    s.add_argument("-o", "--output", default=None,
                   help="write to this file instead of stdout")

    n = sub.add_parser(
        "new",
        help="scaffold an application skeleton from a spec",
        description="Generate a complete, validator-clean application skeleton. "
                    "Never overwrites an existing file; running twice writes nothing.")
    n.add_argument("app_dir", help="directory to generate into")
    n.add_argument("--spec", required=True, help="path to helios-app.yaml")
    n.add_argument("--intake", default=None,
                   help="the answered intake, recorded in the generated README")
    n.add_argument("--dry-run", action="store_true",
                   help="list what would be written without writing anything")
    n.add_argument("--force", action="store_true",
                   help="reserved: currently the scaffolder never overwrites, so "
                        "this flag is accepted and ignored rather than silently "
                        "destroying work")

    v = sub.add_parser(
        "validate",
        help="check an app's spec against its manifests and its code",
        description="Validate the spec's shape and its consistency with the two "
                    "manifests, facts.py and tools.py. Reports every problem at once.")
    v.add_argument("app_dir", help="directory containing helios-app.yaml")
    v.add_argument("--strict", action="store_true",
                   help="treat warnings as failures")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {
        "questions": _questions,
        "spec-from-intake": _spec_from_intake,
        "new": _new,
        "validate": _validate,
    }[args.command]
    try:
        return handler(args)
    except _UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


class _UsageError(RuntimeError):
    """A mistake in how the command was invoked, as opposed to a finding."""


def _questions(args: argparse.Namespace) -> int:
    from .intake import questions_yaml

    text = questions_yaml()
    if args.output:
        _write_output(Path(args.output), text)
    else:
        sys.stdout.write(text)
    return 0


def _spec_from_intake(args: argparse.Namespace) -> int:
    from .intake import IntakeError, spec_from_intake
    from .scaffold import load_intake

    try:
        intake = load_intake(args.intake)
    except Exception as exc:
        raise _UsageError(exc) from exc

    try:
        text = spec_from_intake(intake, app_id=args.app_id)
    except IntakeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.output:
        _write_output(Path(args.output), text)
    else:
        sys.stdout.write(text)
    return 0


def _new(args: argparse.Namespace) -> int:
    from .scaffold import ScaffoldError, load_intake, load_spec, scaffold

    try:
        spec = load_spec(args.spec)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    intake: dict[str, Any] | None = None
    if args.intake:
        try:
            intake = load_intake(args.intake)
        except Exception as exc:
            raise _UsageError(exc) from exc

    if args.force:
        # Accepted and ignored rather than implemented. Overwriting a generated app
        # would destroy the policy and the scenarios someone wrote into it, which is
        # exactly the failure the never-overwrite rule exists to prevent. Saying so
        # is better than silently doing nothing or silently overwriting.
        print("note: --force is ignored; the scaffolder never overwrites an "
              "existing file", file=sys.stderr)

    try:
        result = scaffold(args.app_dir, spec, dry_run=args.dry_run, intake=intake,
                          spec_path=args.spec)
    except ScaffoldError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(result.report())
    if result.dry_run:
        print(f"\n{len(result.written)} file(s) would be written to {result.app_dir}")
        return 0

    print(f"\n{len(result.written)} file(s) written to {result.app_dir}"
          f"{f', {len(result.skipped)} skipped' if result.skipped else ''}")
    print("\nNext: write the policy in "
          f"agents/{spec.app_id}-audit.yaml, then run "
          f"`helios-app validate {result.app_dir}`")
    return 0


def _validate(args: argparse.Namespace) -> int:
    from .validate import validate_app

    app_dir = Path(args.app_dir)
    if not app_dir.is_dir():
        raise _UsageError(f"{app_dir} is not a directory")
    if not (app_dir / "helios-app.yaml").exists():
        raise _UsageError(f"no helios-app.yaml in {app_dir}")

    print(f"checking {app_dir}")
    return validate_app(app_dir).dump(strict=args.strict)


def _write_output(path: Path, text: str) -> None:
    if path.exists():
        # Refusing rather than clobbering, matching the scaffolder's rule: this
        # command's output is usually a spec someone is about to edit by hand.
        raise _UsageError(f"{path} already exists; choose another path or remove it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""check_app.py — validate a generated Helios app before you demo it.

    python3 entry-point/checks/check_app.py entry-point/examples/invoice-payer
    python3 entry-point/checks/check_app.py apps/your-app --strict

This is a thin wrapper. The checks it used to implement now live in
`app_builder.validate`, which is the validator `specs/app-builder.md` specifies;
this file exists so that anything already calling it keeps working.

Prefer `helios-app validate <dir>` (the app-builder console script) for new use.
Same logic, same exit codes, no sys.path juggling:

    .venv/bin/helios-app validate apps/helios-collect

Exit code 0 = clean, 1 = errors, 2 = usage problem. Warnings never fail the run
unless --strict.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for _p in (
    REPO / "platform" / "app-builder" / "src",
    REPO / "platform" / "audit-agent-builder" / "src",
    REPO / "platform" / "action-agent-builder" / "src",
    REPO / "platform" / "financial-tools",
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("app_dir", help="directory containing helios-app.yaml")
    parser.add_argument("--strict", action="store_true",
                        help="treat warnings as failures")
    args = parser.parse_args(argv)

    app_dir = Path(args.app_dir).resolve()
    if not (app_dir / "helios-app.yaml").exists():
        print(f"no helios-app.yaml in {app_dir}")
        return 2

    from app_builder.validate import validate_app

    rel = app_dir.relative_to(REPO) if REPO in app_dir.parents else app_dir
    print(f"checking {rel}")
    return validate_app(app_dir).dump(strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""`helios deploy` — one-line CLI from the App Builder SDK contract.

Zips the app folder (respecting the helios.yaml template) and POSTs it
directly to the Helios Deployment API. No Git repo, no Dockerfile needed.

Usage:
    .venv/bin/python cli/helios.py deploy examples/sample-app --tenant demo
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from pathlib import Path

import httpx

EXCLUDE_DIRS = {"__pycache__", ".venv", "node_modules", ".git", ".helios-run"}
EXCLUDE_FILES = {"Dockerfile"}  # server-side Dockerfile injection owns this


def make_zip(app_dir: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(app_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(app_dir)
            if any(part in EXCLUDE_DIRS for part in rel.parts):
                continue
            if rel.name in EXCLUDE_FILES:
                continue
            zf.write(p, str(rel))
    return buf.getvalue()


def main() -> None:
    ap = argparse.ArgumentParser(prog="helios")
    ap.add_argument("cmd", choices=["deploy"])
    ap.add_argument("app_dir")
    ap.add_argument("--tenant", default="", help="override helios.yaml tenant")
    ap.add_argument("--engine", default="http://127.0.0.1:8100")
    args = ap.parse_args()

    app_dir = Path(args.app_dir).resolve()
    if not app_dir.is_dir():
        sys.exit(f"not a directory: {app_dir}")

    data = make_zip(app_dir)
    try:
        r = httpx.post(
            f"{args.engine}/api/v1/deploy",
            files={"bundle": ("app.zip", data, "application/zip")},
            data={"tenant": args.tenant},
            timeout=600,
        )
    except httpx.HTTPError as exc:
        sys.exit(f"deployment engine unreachable at {args.engine}: {exc}\n"
                 "Is bootstrap.sh running? Engine logs: engine.log")
    try:
        print(json.dumps(r.json(), indent=2))
    except ValueError:
        print(r.text)
    sys.exit(0 if r.status_code == 200 else 1)


if __name__ == "__main__":
    main()

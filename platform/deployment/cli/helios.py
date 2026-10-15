#!/usr/bin/env python3
"""`helios` — command line for the Helios Deployment Engine.

    helios deploy <dir> [--tenant T] [--engine URL] [--include-tests]
    helios apps   [--engine URL]
    helios logs <container> [--tail N] [--engine URL]
    helios undeploy <name> [--tenant T] [--engine URL]
    helios doctor [--engine URL]

No Git repo and no Dockerfile are needed: the engine injects the Dockerfile and
builds on the golden base image.

Exit codes are meaningful, because this CLI is invoked by a coding agent that
has to decide what to do next from the status alone:

    0  success
    1  the engine rejected the request (validation, build, smoke test)
    2  a `.env` was present and would have been shipped -- refused before upload
    3  the engine is unreachable / local diagnostics failed
    4  bad usage (not a directory, unknown container)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import httpx

#: Never bundled. `.env` is handled separately because it needs a hard failure
#: rather than a silent skip -- see `_assert_no_env`.
EXCLUDE_DIRS = {
    "__pycache__", ".venv", "venv", "node_modules", ".git", ".helios-run",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode",
}
#: The engine injects its own Dockerfile, so an app-supplied one is dead weight
#: that would silently be ignored. Excluding it avoids the confusion.
EXCLUDE_FILES = {"Dockerfile"}
#: Excluded by default, overridable with --include-tests. Test files and their
#: recorded runs are development artifacts; shipping them makes a bundle bigger
#: and gives a static-audit hit a larger surface to scan.
TEST_PATH_PARTS = {"tests", "test", "runs"}

DEFAULT_ENGINE = "http://127.0.0.1:8100"


def _iter_files(app_dir: Path, include_tests: bool):
    for p in sorted(app_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(app_dir)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if rel.name in EXCLUDE_FILES:
            continue
        if rel.name == ".env":
            continue  # guarded separately; never bundled even with --include-tests
        if not include_tests and any(part in TEST_PATH_PARTS for part in rel.parts):
            continue
        if not include_tests and rel.name.startswith("test_") and rel.suffix == ".py":
            continue
        yield p, rel


def make_zip(app_dir: Path, include_tests: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p, rel in _iter_files(app_dir, include_tests):
            zf.write(p, str(rel))
    return buf.getvalue()


def _assert_no_env(app_dir: Path) -> None:
    """Refuse to deploy a directory containing a `.env`, unless it is excluded.

    A `.env` normally holds a live API key. Bundling it would upload a secret to
    the engine, and the secret linter would then reject the deploy with a message
    about "possible hardcoded key" that does not explain the real problem.

    Failing here, before any upload, names the cause and the fix. Exit code 2
    distinguishes "you have a secret in the bundle dir" from "the engine
    rejected your app".
    """
    hits = [p for p in app_dir.rglob(".env") if p.is_file()]
    hits += [p for p in app_dir.rglob(".env.*") if p.is_file()]
    # Only those that would actually be bundled matter; anything under an
    # excluded directory never reaches the zip.
    hits = [
        h for h in hits
        if not any(part in EXCLUDE_DIRS for part in h.relative_to(app_dir).parts)
    ]
    if hits:
        rels = ", ".join(str(h.relative_to(app_dir)) for h in sorted(hits))
        sys.exit(
            f"refusing to deploy: {rels} found in {app_dir}\n"
            f"A .env normally holds live credentials, and this CLI never uploads "
            f"secrets.\n"
            f"Move it out of the app directory, or add it to .gitignore and "
            f"delete it. The engine injects runtime secrets from its own "
            f"environment, declared by name in helios.yaml 'env:'.\n"
            f"(exit 2)"
        )


def _engine_url(args: argparse.Namespace) -> str:
    return (getattr(args, "engine", "") or os.environ.get("HELIOS_ENGINE_URL")
            or DEFAULT_ENGINE).rstrip("/")


def _print(payload) -> None:
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(payload)


def _report_failure(response: httpx.Response) -> int:
    """Print an engine rejection in the shape an agent can act on."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        print(response.text)
        return 1
    if isinstance(detail, dict):
        stage = detail.get("stage", "?")
        print(f"[{stage}] {detail.get('error', '')}", file=sys.stderr)
        for problem in detail.get("problems", []) or []:
            print(f"  - {problem}", file=sys.stderr)
        if detail.get("hint"):
            print(f"  hint: {detail['hint']}", file=sys.stderr)
        if detail.get("logs"):
            print("--- logs (tail) ---", file=sys.stderr)
            print(detail["logs"], file=sys.stderr)
    else:
        print(detail, file=sys.stderr)
    return 1


def cmd_deploy(args: argparse.Namespace) -> int:
    app_dir = Path(args.app_dir).resolve()
    if not app_dir.is_dir():
        print(f"not a directory: {app_dir}", file=sys.stderr)
        return 4
    if not (app_dir / "helios.yaml").is_file():
        print(f"{app_dir}/helios.yaml is missing. Create it with at least: "
              f"name: my-app", file=sys.stderr)
        return 4

    _assert_no_env(app_dir)

    data = make_zip(app_dir, include_tests=args.include_tests)
    url = _engine_url(args)
    try:
        r = httpx.post(
            f"{url}/api/v1/deploy",
            files={"bundle": ("app.zip", data, "application/zip")},
            data={"tenant": args.tenant},
            timeout=900,
        )
    except httpx.HTTPError as exc:
        print(f"deployment engine unreachable at {url}: {exc}\n"
              f"Start it with platform/deployment/bootstrap.sh, or point "
              f"--engine at a remote one. Try: helios doctor", file=sys.stderr)
        return 3

    if r.status_code == 200:
        payload = r.json()
        _print(payload)
        for u in payload.get("urls", []):
            print(f"-> {u}", file=sys.stderr)
        return 0
    return _report_failure(r)


def cmd_apps(args: argparse.Namespace) -> int:
    url = _engine_url(args)
    try:
        r = httpx.get(f"{url}/api/v1/apps", timeout=30)
    except httpx.HTTPError as exc:
        print(f"engine unreachable at {url}: {exc}", file=sys.stderr)
        return 3
    if r.status_code != 200:
        return _report_failure(r)
    for a in r.json().get("apps", []):
        print(f"{a['container']:<32} {a['status']:<10} "
              f"role={a.get('role','?'):<9} {a.get('url','')}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    url = _engine_url(args)
    try:
        r = httpx.get(f"{url}/api/v1/apps/{args.container}/logs",
                      params={"tail": args.tail}, timeout=30)
    except httpx.HTTPError as exc:
        print(f"engine unreachable at {url}: {exc}", file=sys.stderr)
        return 3
    if r.status_code != 200:
        return _report_failure(r)
    for line in r.json().get("logs", []):
        print(line)
    return 0


def cmd_undeploy(args: argparse.Namespace) -> int:
    url = _engine_url(args)
    try:
        r = httpx.delete(f"{url}/api/v1/apps/{args.name}",
                         params={"tenant": args.tenant}, timeout=120)
    except httpx.HTTPError as exc:
        print(f"engine unreachable at {url}: {exc}", file=sys.stderr)
        return 3
    if r.status_code != 200:
        return _report_failure(r)
    _print(r.json())
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Local diagnostics. Reports what is wrong, does not try to fix it."""
    url = _engine_url(args)
    problems: list[str] = []

    print(f"engine url: {url}")
    try:
        h = httpx.get(f"{url}/health", timeout=5)
        print(f"engine health: {h.status_code} {h.json()}")
        if h.status_code != 200:
            problems.append("engine /health did not return 200")
    except httpx.HTTPError as exc:
        print(f"engine health: UNREACHABLE ({exc})")
        problems.append("engine unreachable -- start bootstrap.sh")

    # Docker daemon, checked the same way the engine's own bootstrap does.
    sock = os.environ.get("DOCKER_HOST")
    print(f"DOCKER_HOST: {sock or '<unset>'}")
    if sock is None:
        for candidate in (Path.home() / ".orbstack/run/docker.sock",
                          Path.home() / ".colima/default/docker.sock",
                          Path("/var/run/docker.sock")):
            if candidate.exists():
                print(f"socket present: {candidate}")
                break
        else:
            problems.append("no docker socket found")

    try:
        r = httpx.get(f"{url}/api/v1/doctor", timeout=30)
        report = r.json()
    except httpx.HTTPError as exc:
        print(f"doctor endpoint unavailable: {exc}")
        report = {}

    if report:
        print(f"docker: {report.get('docker')}")
        print(f"platform hash: {report.get('platform_hash')}")
        print(f"base image: {report.get('base_image')}")
        print(f"nginx: {report.get('nginx')}")
        if isinstance(report.get("docker"), dict) and "error" in report["docker"]:
            problems.append("docker daemon unreachable from the engine")
        if report.get("nginx") != "running":
            problems.append("helios-nginx container is not running")
        if not (report.get("base_image") or {}).get("present"):
            print("note: base image not built yet; the next deploy builds it")

    if problems:
        print("\nproblems:")
        for p in problems:
            print(f"  - {p}")
        return 3
    print("\ndoctor: OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    # `--engine` is declared on BOTH the top-level parser and a shared parent, so
    # `helios --engine URL apps` and `helios apps --engine URL` both work. The
    # help text documents the second form; accepting only the first would make the
    # documented usage fail, and a coding agent following the help is the common
    # case here.
    #
    # The default is SUPPRESS, not "". With a real default, the subparser
    # re-applies it after the top level has already stored a value, so
    # `--engine URL apps` silently lost the URL. SUPPRESS means "set nothing
    # unless the flag appeared at this level", which leaves whichever level did
    # supply it intact. _engine_url() applies the fallback chain.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--engine", default=argparse.SUPPRESS,
                        help=f"engine base URL (default: $HELIOS_ENGINE_URL or {DEFAULT_ENGINE})")

    ap = argparse.ArgumentParser(prog="helios", description=__doc__,
                                 parents=[common],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("deploy", parents=[common],
                       help="zip an app directory and deploy it")
    p.add_argument("app_dir")
    p.add_argument("--tenant", default="", help="override the helios.yaml tenant")
    p.add_argument("--include-tests", action="store_true",
                   help="bundle tests/ and runs/ (excluded by default)")
    p.set_defaults(func=cmd_deploy)

    p = sub.add_parser("apps", parents=[common], help="list deployed apps")
    p.set_defaults(func=cmd_apps)

    p = sub.add_parser("logs", parents=[common], help="tail a container's logs")
    p.add_argument("container")
    p.add_argument("--tail", type=int, default=200)
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("undeploy", parents=[common],
                       help="remove a deployed app, tenant or container")
    p.add_argument("name")
    p.add_argument("--tenant", default="")
    p.set_defaults(func=cmd_undeploy)

    p = sub.add_parser("doctor", parents=[common],
                       help="diagnose daemon, engine, nginx and base image")
    p.set_defaults(func=cmd_doctor)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

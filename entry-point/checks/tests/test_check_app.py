"""check_app.py is a thin wrapper over `app_builder.validate`.

The checks themselves are tested in platform/app-builder/tests. What this file
pins is the wrapper's contract: it still runs from the repo root with no
installed package, and its exit codes are the ones CI and the build procedure
rely on (0 clean, 1 problems, 2 usage).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "entry-point" / "checks" / "check_app.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-B", str(SCRIPT), *args],
                          capture_output=True, text=True, cwd=REPO)


def test_a_reference_app_is_clean() -> None:
    result = _run("apps/helios-collect")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 error(s)" in result.stdout


def test_the_worked_example_is_clean() -> None:
    result = _run("entry-point/examples/invoice-payer")
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_broken_app_fails_with_every_problem_listed() -> None:
    result = _run("platform/app-builder/tests/fixtures/broken-app")
    assert result.returncode == 1
    assert "error(s)" in result.stdout
    assert "0 error(s)" not in result.stdout


def test_a_directory_without_a_spec_is_a_usage_error(tmp_path: Path) -> None:
    result = _run(str(tmp_path))
    assert result.returncode == 2
    assert "no helios-app.yaml" in result.stdout


def test_strict_turns_warnings_into_failure() -> None:
    lenient = _run("apps/helios-collect")
    strict = _run("apps/helios-collect", "--strict")
    assert lenient.returncode == 0
    if "0 warning(s)" in lenient.stdout:
        assert strict.returncode == 0
    else:
        assert strict.returncode == 1

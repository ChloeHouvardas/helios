"""Validation harness (D8 / P4): scenario suites with declarative expectations.

Spec §7 story: an app (or Qwen during app generation) declares cases —
``{"name", "delta", "expect"}`` — and the harness drives each through
``run_scenario`` and returns a deterministic pass/fail report. This tests a
generated app's financial logic against the engine *before* deployment, with
no randomness anywhere: same suite, same verdict, every time.

Supported ``expect`` predicates (unknown ones raise, they are never ignored):

- ``no_shortfall``: bool — scenario has no first shortfall below 0
- ``min_balance_at_least`` / ``min_balance_at_most``: number
- ``first_shortfall_no_earlier_than``: "YYYY-MM-DD" — first shortfall is None
  (never happens) or on/after that date
- ``final_balance_at_least``: number
"""
from __future__ import annotations

from .engine import first_shortfall, min_balance
from .model import ForecastError, _as_date
from .scenarios import Delta, run_scenario


def _evaluate_expect(scenario, expect: dict, threshold: float) -> list[dict]:
    checks: list[dict] = []
    for predicate, expected in expect.items():
        if predicate == "no_shortfall":
            actual = first_shortfall(scenario, threshold) is None
            checks.append({
                "predicate": predicate, "expected": expected, "actual": actual,
                "passed": actual == bool(expected),
            })
        elif predicate in ("min_balance_at_least", "min_balance_at_most"):
            actual = min_balance(scenario)["value"]
            ok = actual >= expected if predicate == "min_balance_at_least" else actual <= expected
            checks.append({
                "predicate": predicate, "expected": expected, "actual": actual,
                "passed": bool(ok),
            })
        elif predicate == "first_shortfall_no_earlier_than":
            fs = first_shortfall(scenario, threshold)
            actual = fs.isoformat() if fs else None
            ok = fs is None or fs >= _as_date(expected, predicate)
            checks.append({
                "predicate": predicate, "expected": expected, "actual": actual,
                "passed": bool(ok),
            })
        elif predicate == "final_balance_at_least":
            actual = scenario.total[-1]
            checks.append({
                "predicate": predicate, "expected": expected, "actual": actual,
                "passed": bool(actual >= expected),
            })
        else:
            raise ForecastError(
                f"unknown expectation predicate {predicate!r}; supported: "
                "no_shortfall, min_balance_at_least, min_balance_at_most, "
                "first_shortfall_no_earlier_than, final_balance_at_least"
            )
    return checks


def run_validation(
    opening: dict,
    flows: list,
    start,
    days: int,
    cases: list,
    threshold: float = 0.0,
) -> dict:
    """Run a validation suite; returns a deterministic pass/fail report.

    Each case is ``{"name", "delta", "expect"}`` where ``delta`` is the
    declarative scenario (or ``None`` for the baseline) and ``expect`` maps
    predicates to expected values.
    """
    report_cases = []
    for case in cases:
        name = case.get("name")
        if not name:
            raise ForecastError(f"validation case needs a 'name', got {case!r}")
        delta_raw = case.get("delta") or {"operations": []}
        baseline, scenario, _ = run_scenario(
            opening, flows, Delta.parse(delta_raw),
            _as_date(start, "start"), days,
        )
        checks = _evaluate_expect(scenario, case.get("expect") or {}, threshold)
        passed = all(c["passed"] for c in checks)
        report_cases.append({"name": name, "passed": passed, "checks": checks})
    n_passed = sum(1 for c in report_cases if c["passed"])
    return {
        "total": len(report_cases),
        "passed": n_passed,
        "failed": len(report_cases) - n_passed,
        "all_passed": n_passed == len(report_cases),
        "cases": report_cases,
    }

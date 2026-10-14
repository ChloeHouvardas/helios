"""Tests for the validator.

Three kinds of test here, and the split matters:

* **The two real apps validate clean.** They are the regression fixtures — if a new
  check starts firing on them, either the check is wrong or the apps have drifted.
* **The broken-app fixture fires every check exactly once.** Counting errors is not
  enough: a validator can reach 8 errors while two of its checks never run and one
  fires twice. So each check is asserted by name against the message text.
* **Non-vacuity.** A check that passes on everything is indistinguishable from a
  check that is not wired up. Where a check could silently no-op, the test injects
  the defect rather than relying on the fixture.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app_builder.validate import (
    SUBJECT_KEYS,
    Report,
    probe_fact_keys,
    probe_tool_definitions,
    validate_app,
    validate_spec_only,
)

# parents[0] is tests/, parents[3] is the repo root. Getting this wrong yields a
# path that does not exist rather than an obvious failure, so both are asserted
# below rather than trusted.
REPO = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
BROKEN = FIXTURES / "broken-app"
REAL_APPS = [
    REPO / "apps" / "helios-collect",
    REPO / "entry-point" / "examples" / "invoice-payer",
]


def test_the_fixture_paths_actually_exist() -> None:
    """Guards the constants above. A wrong parents[] index fails 25 tests at once
    with a message about a missing file, which reads like a fixture problem."""
    assert BROKEN.is_dir(), BROKEN
    assert (BROKEN / "helios-app.yaml").is_file()
    for app in REAL_APPS:
        assert app.is_dir(), app


# ---------------------------------------------------------------------------
# The real apps are clean
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("app", REAL_APPS, ids=lambda p: p.name)
def test_a_real_app_validates_with_no_errors(app: Path) -> None:
    """Both checked-in apps must stay clean, or the validator has become noise."""
    report = validate_app(app)
    assert report.errors == [], "\n".join(report.errors)
    assert report.exit_code == 0


@pytest.mark.parametrize("app", REAL_APPS, ids=lambda p: p.name)
def test_a_real_app_reports_only_advisory_warnings(app: Path) -> None:
    """Warnings are allowed, but each must be about graph shape, not a defect.

    Both apps publish entity types they do not declare locally (`negotiation`,
    `payment`) and declare no reads. That is genuine cross-app state, and the
    warning exists to make a typo visible — not to demand a fix.
    """
    report = validate_app(app)
    for w in report.warnings:
        assert "graph" in w or "subject key" in w or "facts.py" in w, w


def test_helios_collect_fact_source_supplies_every_key_its_rules_need() -> None:
    """The strongest real-world check: a correct app is not reported as broken.

    This is the test that would have caught the naive implementation. Calling
    `extra_facts_for("dummy")` on helios-collect returns an empty dict, because a
    fact source must return None for a subject it does not know — so a dynamic
    probe reports every key as missing and fails an app that is right.
    """
    app = REPO / "apps" / "helios-collect"
    keys, methods, err = probe_fact_keys(app / "facts.py")
    assert err is None, err
    assert keys, "extra_facts_for produces no keys — the probe is not reading it"
    # The keys its audit manifest actually binds.
    for needed in ("amount_due", "invoice_amount_cents", "not_yet_due",
                   "invoice_disputed"):
        assert needed in keys, f"{needed!r} not found in {sorted(keys)}"
    assert "record_contact" in methods


def test_helios_collect_tools_agree_with_its_audit_manifest() -> None:
    """Side-effect flags cross-check correctly on a real app, in both directions."""
    app = REPO / "apps" / "helios-collect"
    found, err = probe_tool_definitions(app / "tools.py")
    assert err is None, err
    # The four tools built locally. `send_email` comes from the platform's
    # make_send_email and is invisible to an AST scan by construction.
    assert found == {
        "list_overdue_invoices": False,
        "lookup_invoice": False,
        "record_agreed_extension": True,
        "record_inbound_payment": True,
    }


# ---------------------------------------------------------------------------
# The broken app: every check fires, exactly once
# ---------------------------------------------------------------------------

def test_the_broken_fixture_exits_nonzero() -> None:
    assert validate_app(BROKEN).exit_code == 1


def test_the_broken_fixture_produces_at_least_eight_distinct_errors() -> None:
    report = validate_app(BROKEN)
    assert len(report.errors) >= 8, "\n".join(report.errors)
    # Distinct, not eight copies of one problem.
    assert len(set(report.errors)) == len(report.errors)


#: One entry per check, with a substring that only that check can produce. This is
#: the table that keeps the fixture honest: if a check stops running, its row fails
#: rather than the total quietly dropping to seven.
#:
#: Each needle must be phrasing unique to ONE check, not a tool name. Tool names
#: appear in several messages — the "not a superset" error lists every missing tool,
#: so a needle of `misflagged_write` matches two errors and the test fails for a
#: reason that has nothing to do with the check it is meant to cover.
EXPECTED_ERRORS = [
    ("undeclared tool", "has not declared: ['undeclared_tool']"),
    ("spec tools not a superset", "not a superset of both manifests"),
    ("identity mismatch", "!= audit manifest app_id"),
    ("no task allows anything", "denied everything at authorize()"),
    ("rule binds an unknown argument", "binds argument(s) ['ghost_argument']"),
    ("rule needs a missing fact", "fact key(s) ['invoice_amount_cents']"),
    ("side-effect flag disagrees (unsafe direction)",
     "'misflagged_write' is side_effect=True in tools.py"),
    ("side-effect flag disagrees (safe direction)",
     "'misflagged_read' is side_effect=False in tools.py"),
]


@pytest.mark.parametrize("label,needle", EXPECTED_ERRORS, ids=[e[0] for e in EXPECTED_ERRORS])
def test_each_expected_check_fires_exactly_once(label: str, needle: str) -> None:
    report = validate_app(BROKEN)
    hits = [e for e in report.errors if needle in e]
    assert len(hits) == 1, (
        f"{label}: expected exactly 1 error mentioning {needle!r}, got {len(hits)}"
        + ("\n".join(report.errors) if not hits else ""))


EXPECTED_WARNINGS = [
    ("entity with no fields", "declares no fields"),
    ("integer money not named _cents", "total_cents"),
    ("no approval_queue page", "no approval_queue page"),
    ("published entity not declared locally", "'ghost'"),
    ("publishes without reads", "publishes but no reads"),
    ("action name differs from app_id", "differs from audit app_id"),
    ("side-effect tool no rule mentions", "no app rule mentioning it"),
    ("gated tool with no subject key", "no subject key"),
]


@pytest.mark.parametrize("label,needle", EXPECTED_WARNINGS,
                         ids=[w[0] for w in EXPECTED_WARNINGS])
def test_each_expected_warning_fires_exactly_once(label: str, needle: str) -> None:
    report = validate_app(BROKEN)
    hits = [w for w in report.warnings if needle in w]
    assert len(hits) == 1, f"{label}: expected 1 warning with {needle!r}, got {len(hits)}"


def test_the_unsafe_flag_mismatch_says_what_is_dangerous_about_it() -> None:
    """The two mismatch directions must not read the same.

    manifest=false / app=true is a control bypass: the side effect runs with no
    proposal and no owner. manifest=true / app=false is a nuisance. A message that
    does not distinguish them sends the reader to fix the wrong one.
    """
    report = validate_app(BROKEN)
    unsafe = next(e for e in report.errors
                  if "'misflagged_write' is side_effect=True" in e)
    safe = next(e for e in report.errors
                if "'misflagged_read' is side_effect=False" in e)
    assert "ungated side effect" in unsafe
    assert "ungated side effect" not in safe


# ---------------------------------------------------------------------------
# Non-vacuity: the checks actually run
# ---------------------------------------------------------------------------

def test_the_broken_fixture_spec_is_itself_valid() -> None:
    """Load-bearing: a spec with shape errors would skip every cross-file check.

    If someone "improves" the fixture by breaking its spec too, the error count
    stays high while the checks that matter stop running. This test pins the
    precondition.
    """
    report = validate_spec_only((BROKEN / "helios-app.yaml").read_text(encoding="utf-8"))
    assert report.errors == [], "\n".join(report.errors)


def test_the_broken_fixture_tools_and_facts_both_import() -> None:
    """If either failed to import, two checks would degrade to warnings."""
    keys, methods, err = probe_fact_keys(BROKEN / "facts.py")
    assert err is None, err
    assert keys, "the fixture fact source should produce some keys"
    assert "record_contact" in methods

    _, tools_err = probe_tool_definitions(BROKEN / "tools.py")
    assert tools_err is None, tools_err


def test_the_fact_check_is_not_vacuous_on_the_broken_fixture() -> None:
    """The fixture supplies some keys and omits one — so the check discriminates.

    A fact source that produced nothing would make "missing key" trivially true and
    would not prove the check can tell a supplied key from an unsupplied one.
    """
    keys, _, _ = probe_fact_keys(BROKEN / "facts.py")
    assert "amount_due" in keys, "the fixture should supply some keys"
    assert "invoice_amount_cents" not in keys, (
        "the fixture's whole defect is that this key is missing")


def test_subject_keys_match_the_real_resolver_constant() -> None:
    """The local copy must not drift from audit_agent's.

    SUBJECT_KEYS is duplicated so the validator works without audit_agent
    installed. Duplication without a drift check is how a stale constant becomes a
    silently wrong one.
    """
    pytest.importorskip("audit_agent.facts")
    from audit_agent.facts import FactResolver
    assert SUBJECT_KEYS == FactResolver.SUBJECT_KEYS


# ---------------------------------------------------------------------------
# Degraded inputs fail closed rather than crashing
# ---------------------------------------------------------------------------

def test_a_missing_app_directory_is_reported_not_raised(tmp_path: Path) -> None:
    report = validate_app(tmp_path / "nope")
    assert report.exit_code == 1
    assert any("no helios-app.yaml" in e for e in report.errors)


def test_a_broken_spec_stops_before_the_cross_file_checks(tmp_path: Path) -> None:
    """Shape problems make every cross-file check meaningless, so say that."""
    (tmp_path / "helios-app.yaml").write_text("spec_version: '9'\n", encoding="utf-8")
    report = validate_app(tmp_path)
    assert report.exit_code == 1
    assert any("skipped" in e for e in report.errors), report.errors


def test_a_missing_manifest_file_is_reported_with_its_path(tmp_path: Path) -> None:
    spec = (BROKEN / "helios-app.yaml").read_text(encoding="utf-8")
    (tmp_path / "helios-app.yaml").write_text(spec, encoding="utf-8")
    report = validate_app(tmp_path)
    assert any("missing file" in e for e in report.errors), report.errors


def test_an_invalid_action_manifest_yields_one_error_not_a_traceback(tmp_path: Path) -> None:
    import shutil
    shutil.copytree(BROKEN, tmp_path / "app")
    (tmp_path / "app" / "agents" / "broken-app-action.json").write_text(
        "{not json", encoding="utf-8")
    report = validate_app(tmp_path / "app")
    assert report.exit_code == 1
    assert any("not valid JSON" in e for e in report.errors), report.errors


def test_an_unparseable_facts_py_degrades_to_a_warning(tmp_path: Path) -> None:
    """A broken facts.py must not take the validator down with it."""
    import shutil
    shutil.copytree(BROKEN, tmp_path / "app")
    (tmp_path / "app" / "facts.py").write_text(
        "def extra_facts_for(:  # syntax error\n", encoding="utf-8")
    report = validate_app(tmp_path / "app")
    # Still reports the rest; the fact check becomes a warning, not a crash.
    assert any("undeclared_tool" in e for e in report.errors)


def test_a_relative_app_path_resolves_correctly(monkeypatch) -> None:
    """Regression: the import probe used to double-resolve relative paths.

    A relative path was resolved once against the caller's cwd and again against
    the subprocess's cwd (set to the app dir), producing
    `apps/x/apps/x/facts.py`. The AST side worked, so only the import probe
    failed — and it failed as a warning, which is easy to overlook.

    Run from the repo root, because that is the cwd a caller uses in practice and
    this suite runs from the package directory.
    """
    monkeypatch.chdir(REPO)
    keys, _, err = probe_fact_keys(Path("apps/helios-collect/facts.py"))
    assert err is None, f"relative path broke the import probe: {err}"
    assert keys

    report = validate_app(Path("apps/helios-collect"))
    assert report.errors == [], "\n".join(report.errors)


# ---------------------------------------------------------------------------
# Report behaviour
# ---------------------------------------------------------------------------

def test_a_clean_report_exits_zero_and_prints_counts(capsys) -> None:
    r = Report()
    assert r.dump() == 0
    assert "0 error(s), 0 warning(s)" in capsys.readouterr().out


def test_strict_turns_warnings_into_failures(capsys) -> None:
    r = Report()
    r.warn("advisory")
    assert r.dump() == 0
    assert r.dump(strict=True) == 1
    assert "--strict" in capsys.readouterr().out

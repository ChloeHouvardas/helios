"""Tests for the `agent-toolkit` CLI.

Each command's exit code is the contract a pipeline depends on: 0 clean, 1 problems
found, 2 usage error. Those are the same codes `entry-point/checks/check_app.py`
and `helios-app` use, so a caller can treat all three alike.

`pyproject.toml` has declared this console script since the package was created,
but the module did not exist — so the installed script crashed with
ModuleNotFoundError on every invocation. `test_the_console_script_is_installed_and_runs`
is the regression test for that specific failure, which no other test here would
catch: every other test calls `main()` directly and would pass with a broken entry
point.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_toolkit.cli import main

REPO = Path(__file__).resolve().parents[3]
COLLECT = REPO / "apps" / "helios-collect"
PAYER = REPO / "entry-point" / "examples" / "invoice-payer"
BROKEN = REPO / "platform" / "app-builder" / "tests" / "fixtures" / "broken-app"

COLLECT_ACTION = COLLECT / "agents" / "helios-collect-action.json"
COLLECT_AUDIT = COLLECT / "agents" / "helios-collect-audit.yaml"
PAYER_ACTION = PAYER / "agents" / "invoice-payer-action.json"
PAYER_AUDIT = PAYER / "agents" / "invoice-payer-audit.yaml"
BROKEN_ACTION = BROKEN / "agents" / "broken-app-action.json"
BROKEN_AUDIT = BROKEN / "agents" / "broken-app-audit.yaml"


# ---------------------------------------------------------------------------
# The entry point itself
# ---------------------------------------------------------------------------

def test_the_console_script_is_installed_and_runs() -> None:
    """Regression: the declared entry point used to crash on import.

    Called as a subprocess, because invoking `main()` in-process cannot detect a
    broken console-script wiring — that is exactly the failure this guards.
    """
    proc = subprocess.run([sys.executable, "-m", "agent_toolkit.cli", "--help"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "inspect" in proc.stdout


def test_the_console_script_entry_point_is_declared() -> None:
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8")
    assert 'agent-toolkit = "agent_toolkit.cli:main"' in pyproject


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------

def test_inspect_reports_the_tools_mandate_and_turn_budget(capsys) -> None:
    assert main(["inspect", str(COLLECT_ACTION)]) == 0
    out = capsys.readouterr().out
    assert "helios-collect" in out
    assert "max_turns:  12" in out
    assert "send_email" in out
    assert "record_inbound_payment" in out


def test_inspect_shows_the_mandate_limits(capsys) -> None:
    main(["inspect", str(COLLECT_ACTION)])
    assert "min_days_between_contacts" in capsys.readouterr().out


def test_inspect_json_is_machine_readable(capsys) -> None:
    assert main(["inspect", "--json", str(COLLECT_ACTION)]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["name"] == "helios-collect"
    assert document["max_turns"] == 12
    assert "send_email" in document["mandate"]["allowed_tools"]


def test_inspect_marks_a_tool_that_is_not_in_the_mandate(tmp_path: Path, capsys) -> None:
    document = json.loads(COLLECT_ACTION.read_text(encoding="utf-8"))
    document["mandate"]["allowed_tools"] = ["lookup_invoice"]
    path = tmp_path / "action.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    assert main(["inspect", str(path)]) == 0
    assert "not in mandate.allowed_tools" in capsys.readouterr().out


def test_inspect_says_when_there_are_no_numeric_limits(tmp_path: Path, capsys) -> None:
    """An empty limits block means the only bound is which tools may be called,
    not how large an action may be — worth saying rather than printing `{}`."""
    document = json.loads(COLLECT_ACTION.read_text(encoding="utf-8"))
    document["mandate"]["limits"] = {}
    path = tmp_path / "action.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    main(["inspect", str(path)])
    out = capsys.readouterr().out
    assert "no numeric limits" in out


def test_inspect_a_missing_file_is_a_usage_error(capsys, tmp_path: Path) -> None:
    assert main(["inspect", str(tmp_path / "nope.json")]) == 2
    assert "could not be loaded" in capsys.readouterr().err


def test_inspect_an_invalid_manifest_is_a_usage_error(tmp_path: Path, capsys) -> None:
    path = tmp_path / "action.json"
    path.write_text('{"name": "x"}', encoding="utf-8")
    assert main(["inspect", str(path)]) == 2


def test_inspect_a_manifest_with_an_extra_key_is_refused(tmp_path: Path, capsys) -> None:
    """`extra="forbid"`: a `_comment` is rejected, which is the surprise worth testing."""
    document = json.loads(COLLECT_ACTION.read_text(encoding="utf-8"))
    document["_comment"] = "harmless?"
    path = tmp_path / "action.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert main(["inspect", str(path)]) == 2


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", [COLLECT_ACTION, PAYER_ACTION],
                         ids=["helios-collect", "invoice-payer"])
def test_validate_accepts_a_real_action_manifest(action: Path, capsys) -> None:
    assert main(["validate", str(action)]) == 0
    assert "0 error(s)" in capsys.readouterr().out


def test_validate_with_audit_accepts_both_real_apps(capsys) -> None:
    assert main(["validate", str(COLLECT_ACTION), "--audit", str(COLLECT_AUDIT)]) == 0
    capsys.readouterr()
    assert main(["validate", str(PAYER_ACTION), "--audit", str(PAYER_AUDIT)]) == 0
    assert "0 error(s)" in capsys.readouterr().out


def test_validate_reports_an_undeclared_tool(tmp_path: Path, capsys) -> None:
    """THE invariant: an undeclared tool is not denied, it is never looked at."""
    action = json.loads(COLLECT_ACTION.read_text(encoding="utf-8"))
    action["tools"].append("wipe_the_database")
    action["mandate"]["allowed_tools"].append("wipe_the_database")
    path = tmp_path / "action.json"
    path.write_text(json.dumps(action), encoding="utf-8")

    assert main(["validate", str(path), "--audit", str(COLLECT_AUDIT)]) == 1
    out = capsys.readouterr().out
    assert "wipe_the_database" in out
    assert "has not declared" in out
    assert "no gate" in out


def test_validate_reports_a_tool_the_agent_cannot_call(tmp_path: Path, capsys) -> None:
    """The reverse direction: dead policy is a defect too, just a safe one."""
    action = json.loads(COLLECT_ACTION.read_text(encoding="utf-8"))
    action["tools"] = ["lookup_invoice"]
    action["mandate"]["allowed_tools"] = ["lookup_invoice"]
    path = tmp_path / "action.json"
    path.write_text(json.dumps(action), encoding="utf-8")

    assert main(["validate", str(path), "--audit", str(COLLECT_AUDIT)]) == 1
    assert "does not offer" in capsys.readouterr().out


def test_validate_reports_an_identity_mismatch(tmp_path: Path, capsys) -> None:
    action = json.loads(COLLECT_ACTION.read_text(encoding="utf-8"))
    action["name"] = "some-other-app"
    path = tmp_path / "action.json"
    path.write_text(json.dumps(action), encoding="utf-8")

    assert main(["validate", str(path), "--audit", str(COLLECT_AUDIT)]) == 1
    out = capsys.readouterr().out
    assert "some-other-app" in out
    assert "app_id" in out


def test_validate_reports_a_gated_tool_outside_the_mandate(tmp_path: Path, capsys) -> None:
    """A side-effecting tool the agent can never be granted is dead policy."""
    action = json.loads(COLLECT_ACTION.read_text(encoding="utf-8"))
    action["mandate"]["allowed_tools"] = [
        t for t in action["mandate"]["allowed_tools"] if t != "send_email"]
    path = tmp_path / "action.json"
    path.write_text(json.dumps(action), encoding="utf-8")

    assert main(["validate", str(path), "--audit", str(COLLECT_AUDIT)]) == 1
    assert "can never be granted" in capsys.readouterr().out


def test_validate_on_the_broken_fixture_reports_the_undeclared_tool(capsys) -> None:
    assert main(["validate", str(BROKEN_ACTION), "--audit", str(BROKEN_AUDIT)]) == 1
    out = capsys.readouterr().out
    assert "undeclared_tool" in out
    assert "has not declared" in out


def test_validate_quiet_prints_only_problems(capsys) -> None:
    assert main(["validate", "--quiet", str(COLLECT_ACTION), "--audit",
                 str(COLLECT_AUDIT)]) == 0
    out = capsys.readouterr().out
    assert "ok" not in out
    assert "0 error(s)" in out


def test_validate_a_missing_action_manifest_is_a_finding_not_a_crash(
        capsys, tmp_path: Path) -> None:
    assert main(["validate", str(tmp_path / "nope.json")]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_validate_an_invalid_audit_manifest_reports_one_error(capsys, tmp_path: Path) -> None:
    audit = tmp_path / "audit.yaml"
    audit.write_text("app_id: x\n", encoding="utf-8")
    assert main(["validate", str(COLLECT_ACTION), "--audit", str(audit)]) == 1
    out = capsys.readouterr().out
    assert "invalid audit manifest" in out
    assert "1 error(s)" in out


def test_validate_without_audit_does_not_check_cross_file(capsys, tmp_path: Path) -> None:
    """Without --audit there is nothing to compare against, so an app whose audit
    manifest disagrees still passes — and that is documented, not hidden."""
    action = json.loads(COLLECT_ACTION.read_text(encoding="utf-8"))
    action["tools"].append("wipe_the_database")
    path = tmp_path / "action.json"
    path.write_text(json.dumps(action), encoding="utf-8")

    assert main(["validate", str(path)]) == 0


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------

def test_demo_exits_zero(tmp_path: Path) -> None:
    assert main(["demo", "--run-dir", str(tmp_path)]) == 0


def test_demo_produces_all_four_outcomes(tmp_path: Path, capsys) -> None:
    """The demo has to show the whole decision space, not just the happy path.

    A demo of approvals only would not demonstrate that the control works when the
    agent misbehaves, which is the only case the control exists for.
    """
    main(["demo", "--run-dir", str(tmp_path)])
    out = capsys.readouterr().out
    for marker in ("[ALLOW]", "[HOLD ]", "[DENY ]"):
        assert marker in out, marker
    assert "undeclared" in out
    assert "unknown_tool" in out


def test_demo_reports_no_unexpected_verdict(tmp_path: Path, capsys) -> None:
    """The demo asserts its own expectations; a gate that changed behaviour must
    not still print a tidy run."""
    main(["demo", "--run-dir", str(tmp_path)])
    assert "UNEXPECTED" not in capsys.readouterr().err


def test_demo_writes_an_action_log(tmp_path: Path) -> None:
    main(["demo", "--run-dir", str(tmp_path)])
    log = tmp_path / "action-log.jsonl"
    assert log.is_file()
    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    assert entries, "the demo wrote no log entries"


def test_demo_logs_every_step_in_order(tmp_path: Path) -> None:
    """Append-only, and written before the next decision rather than after."""
    main(["demo", "--run-dir", str(tmp_path)])
    entries = [json.loads(line)
               for line in (tmp_path / "action-log.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    kinds = [e["kind"] for e in entries]
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"
    for kind in ("tool_call", "proposal", "verdict", "action_result", "hold",
                 "resumed"):
        assert kind in kinds, kind
    # Sequence numbers are monotonic: the log cannot have been reordered.
    assert [e["seq"] for e in entries] == sorted(e["seq"] for e in entries)


def test_demo_denies_and_holds_without_executing(tmp_path: Path) -> None:
    """Only the allowed proposal reaches a customer."""
    main(["demo", "--run-dir", str(tmp_path)])
    entries = [json.loads(line)
               for line in (tmp_path / "action-log.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    results = [e for e in entries if e["kind"] == "action_result"]
    executed = [e for e in results if e.get("executed")]
    assert len(executed) == 1, [e.get("tool") for e in results]
    assert not [e for e in results if not e.get("executed") and "result" in e], (
        "a refused proposal must not carry a result")


def test_demo_saves_a_resumable_hold(tmp_path: Path) -> None:
    """The held proposal must round-trip, not merely produce a file."""
    main(["demo", "--run-dir", str(tmp_path)])
    holds = sorted((tmp_path / "demo-run-1" / "holds").glob("*.json"))
    assert len(holds) == 1
    record = json.loads(holds[0].read_text(encoding="utf-8"))
    assert record["proposal"]["tool"] == "send_email"
    assert record["proposal"]["arguments"]["invoice_id"] == "INV-1003"
    assert record["reason"]


def test_demo_json_is_machine_readable(tmp_path: Path, capsys) -> None:
    assert main(["demo", "--json", "--run-dir", str(tmp_path)]) == 0
    document = json.loads(capsys.readouterr().out)
    assert {r["label"] for r in document["results"]} == {
        "allowed", "held", "denied", "undeclared"}
    by_label = {r["label"]: r for r in document["results"]}
    assert by_label["allowed"]["decision"] == "approve"
    assert by_label["held"]["decision"] == "hold"
    assert by_label["denied"]["decision"] == "reject"
    assert by_label["undeclared"]["rule"] == "unknown_tool"
    assert document["resumed"]["tool"] == "send_email"


def test_demo_is_reproducible(tmp_path: Path, capsys) -> None:
    """Fixed date, no model, no network: two runs must agree on every verdict."""
    main(["demo", "--json", "--run-dir", str(tmp_path / "a")])
    first = json.loads(capsys.readouterr().out)
    main(["demo", "--json", "--run-dir", str(tmp_path / "b")])
    second = json.loads(capsys.readouterr().out)

    def decisions(document):
        return [(r["label"], r["decision"], r["rule"]) for r in document["results"]]

    assert decisions(first) == decisions(second)


def test_demo_needs_no_network_and_no_api_key(tmp_path: Path, monkeypatch) -> None:
    """A demo that needs a key is not a demo; it is an integration test."""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert main(["demo", "--run-dir", str(tmp_path)]) == 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def test_no_command_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_an_unknown_command_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["frobnicate"])
    assert excinfo.value.code == 2


@pytest.mark.parametrize("command", ["inspect", "validate"])
def test_a_command_without_its_manifest_argument_is_a_usage_error(command: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([command])
    assert excinfo.value.code == 2

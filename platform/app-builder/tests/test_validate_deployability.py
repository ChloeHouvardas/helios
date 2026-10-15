"""An unguarded module-level parents[N] fails inside the app image; warn on it."""
from __future__ import annotations

from pathlib import Path

from app_builder.validate import Report, check_deployability


def test_unguarded_parents_is_warned(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("HERE = Path(__file__).resolve().parent\nREPO = HERE.parents[1]\n")
    (tmp_path / "server.py").write_text("REPO = HERE.parents[1] if len(HERE.parents) > 1 else HERE\n")
    r = Report()
    check_deployability(tmp_path, r)
    assert len(r.warnings) == 1 and "app.py:2" in r.warnings[0]
    assert r.errors == []


def test_the_flagships_are_guarded() -> None:
    repo = Path(__file__).resolve().parents[3]
    for app in ("helios-collect", "helios-payables", "helios-cash-planner"):
        r = Report()
        check_deployability(repo / "apps" / app, r)
        assert r.warnings == [], (app, r.warnings)

"""Tests for check_app.py's side-effect cross-check.

The checker's own docstring promised this cross-check before the code performed
it — the gap was invisible precisely because the app that ships with the repo
happens to agree on every flag. So these tests build the disagreement on
purpose: they assert the check *fires*, which is the only way to know it exists.

Offline, and deliberately cheap: the platform catalogs are read for their
declared flags only, never invoked.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

CHECKS = Path(__file__).resolve().parents[1]
if str(CHECKS) not in sys.path:
    sys.path.insert(0, str(CHECKS))

from check_app import (  # noqa: E402
    Report,
    _action_side_flags,
    _check_side_effect_agreement,
)


@dataclass(frozen=True)
class _DeclaredTool:
    """Stands in for an audit-manifest tool entry: only `side_effect` is read."""

    name: str
    side_effect: bool


def _declared(**flags: bool) -> dict:
    return {name: _DeclaredTool(name, flag) for name, flag in flags.items()}


def _errors(**audit_side_flags: bool) -> list[str]:
    """Report errors for tools whose AUDIT side is declared as given.

    The action side is not a parameter: it always comes from the real platform
    catalogs, which is the whole point — the check compares the audit manifest
    against the source of truth the runtime actually gates on. So a test
    produces a disagreement by declaring an audit flag that contradicts the
    catalog, and these are the catalog values that matter here:

        payments.send    -> action side True
        payments.balance -> action side False
        email.send       -> action side True
        lookup_invoice   -> action side False
    """
    r = Report()
    _check_side_effect_agreement(
        set(audit_side_flags), _declared(**audit_side_flags), r
    )
    return r.errors


# --- the catalogs this check reads from ------------------------------------


def test_action_side_flags_resolve_the_real_catalogs() -> None:
    """The check reads the same catalogs the runtime gates on.

    If a catalog import broke, every app would silently pass with nothing
    checked, so this asserts the real tools are present with real flags rather
    than trusting an empty dict.
    """
    flags, problems = _action_side_flags()
    assert problems == []
    assert flags["payments.send"] is True
    assert flags["payments.balance"] is False
    assert flags["email.send"] is True
    assert flags["lookup_invoice"] is False


def test_reading_the_catalogs_does_not_mutate_the_fin_tools_global() -> None:
    """The validator must be side-effect free, as specs/app-builder.md requires.

    `register()` accepts an explicit registry; `bind()` is what mutates global
    handler state and is never called. Reading flags must not populate the
    module-global REGISTRY, or a checker run would change what a later
    in-process app sees.
    """
    import fin_tools

    before = len(fin_tools.REGISTRY.list())
    _action_side_flags()
    assert len(fin_tools.REGISTRY.list()) == before


# --- the disagreement the check exists to catch ----------------------------


def test_a_tool_the_auditor_gates_but_the_action_side_does_not_is_an_error() -> None:
    """The dangerous direction, and the reason this check exists.

    The runtime gates on the ACTION side's flag, so a tool it believes is
    read-only runs directly and never reaches the auditor. The audit manifest's
    rules for it become dead text while the side effect happens ungated.

    `payments.balance` is read-only on the action side; declaring it
    side-effecting in the audit manifest is the disagreement.
    """
    errors = _errors(**{"payments.balance": True})
    assert len(errors) == 1
    assert "payments.balance" in errors[0]
    # The message must explain the consequence, not just the mismatch, because
    # it is read by a model that has to decide what to change.
    assert "never reaches the auditor" in errors[0]


def test_a_tool_the_action_side_gates_but_the_auditor_does_not_is_an_error() -> None:
    """The other direction: the auditor would be asked to adjudicate something
    it believes cannot change state.

    `payments.send` is side-effecting on the action side; declaring it
    read-only in the audit manifest is the disagreement.
    """
    errors = _errors(**{"payments.send": False})
    assert len(errors) == 1
    assert "payments.send" in errors[0]
    assert "cannot change state" in errors[0]


def test_agreeing_flags_produce_no_error() -> None:
    """The happy path, so the tests above cannot pass by always erroring."""
    assert _errors(**{
        "payments.send": True,
        "payments.balance": False,
        "email.send": True,
        "lookup_invoice": False,
    }) == []


def test_every_disagreeing_tool_is_reported_not_just_the_first() -> None:
    """The checker reports all problems at once; a revision loop that fixes one
    error per round trip is slow."""
    errors = _errors(**{"payments.send": False, "payments.balance": True})
    assert len(errors) == 2
    assert {"payments.send", "payments.balance"} == {
        e.split("'")[1] for e in errors
    }


def test_a_tool_the_checker_cannot_resolve_is_a_warning_not_a_silent_pass() -> None:
    """An app-local tool lives in the app's own code, which this checker does
    not import. Saying so is a warning; silently passing it would be a hole."""
    r = Report()
    _check_side_effect_agreement(
        {"app_local_tool"}, _declared(**{"app_local_tool": True}), r
    )
    assert r.errors == []
    assert len(r.warnings) == 1
    assert "app_local_tool" in r.warnings[0]
    assert "app-local" in r.warnings[0]


def test_a_tool_only_one_side_knows_about_is_not_compared() -> None:
    """A tool absent from the audit manifest is a different check's job (the
    subset check). This one must not double-report it as a flag disagreement."""
    r = Report()
    _check_side_effect_agreement(
        {"payments.send"}, _declared(**{"payments.balance": False}), r
    )
    assert r.errors == []
    assert r.warnings == []

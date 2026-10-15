"""Tests for the UI kit.

The load-bearing requirement is that every pattern renders with a MINIMAL context.
A generated app that renders "undefined" in an empty table reads as broken however
good the rest of it looks, so each pattern has to tolerate being handed nothing.

The masking tests matter more than they look. The approval queue is the one page
where a mistake puts a credential on a screen in front of a human, and `auth_code`
— exactly what a payment proposal carries — is not in the platform's REDACTED_KEYS.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from jinja2 import StrictUndefined, TemplateNotFound

from app_builder.kit import (
    HELD_ACTION_PARTIAL,
    PATTERNS,
    SENSITIVE_KEYS,
    build_env,
    kit_dir,
    mask_arguments,
    missing_patterns,
    render_pattern,
    template_for_pattern,
    validate_pattern_set,
)
from app_builder.spec import UI_PATTERNS, AppSpec

REPO = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# The kit is complete
# ---------------------------------------------------------------------------

def test_every_known_pattern_has_a_template() -> None:
    """A pattern with no template generates a page that renders nothing."""
    assert validate_pattern_set() is None
    for pattern, filename in PATTERNS.items():
        assert (kit_dir() / filename).is_file(), f"{pattern} -> {filename} is missing"


def test_the_kit_maps_exactly_the_spec_closed_set() -> None:
    """The kit and the spec model must not drift; a new pattern needs both halves."""
    assert set(PATTERNS) == set(UI_PATTERNS)


def test_the_held_action_partial_ships() -> None:
    assert (kit_dir() / HELD_ACTION_PARTIAL).is_file()


def test_the_base_layout_ships() -> None:
    assert (kit_dir() / "base.html.j2").is_file()


def test_the_design_tokens_ship() -> None:
    tokens = kit_dir() / "static" / "tokens.css"
    assert tokens.is_file()
    text = tokens.read_text(encoding="utf-8")
    # Light and dark come from the same tokens, so both blocks must exist.
    assert ":root" in text
    assert "prefers-color-scheme: dark" in text


def test_the_vendored_htmx_ships_in_the_kit_static_dir() -> None:
    """A generated app copies its static assets from here, so it must be present."""
    assert (kit_dir() / "static" / "htmx.min.js").is_file()


def test_missing_patterns_reports_only_absent_templates() -> None:
    assert missing_patterns(sorted(PATTERNS)) == []
    assert missing_patterns([]) == []


def test_an_unknown_pattern_is_refused_with_the_allowed_list() -> None:
    with pytest.raises(KeyError) as excinfo:
        template_for_pattern("dashboard")
    assert "approval_queue" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Every pattern renders with a minimal context
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pattern", sorted(PATTERNS), ids=sorted(PATTERNS))
def test_every_pattern_renders_with_an_empty_context(pattern: str) -> None:
    """The requirement: a pattern handed nothing still produces a valid page."""
    html = render_pattern(pattern, {})
    assert html.strip(), f"{pattern} rendered nothing"
    assert "<!doctype html>" in html.lower()
    assert "tokens.css" in html


@pytest.mark.parametrize("pattern", sorted(PATTERNS), ids=sorted(PATTERNS))
def test_every_pattern_renders_without_swallowing_typos(pattern: str) -> None:
    """StrictUndefined catches a template that references a variable nobody passes.

    With the default Undefined a typo renders as an empty string, so a broken
    template looks fine in tests and shows a blank cell in production. This is the
    check that a generated app should run during development.
    """
    env = build_env(strict=True)
    # A page context, not an empty one: strict mode still has to render the
    # realistic case. Anything the template requires and cannot default is a bug.
    html = env.get_template(PATTERNS[pattern]).render(
        app_name="Test App", pages=[], heading="H", error=None)
    assert "<!doctype html>" in html.lower()


@pytest.mark.parametrize("pattern", sorted(PATTERNS), ids=sorted(PATTERNS))
def test_every_pattern_shows_an_empty_state_rather_than_a_broken_page(pattern: str) -> None:
    """No data must read as "nothing here", not as an empty region."""
    html = render_pattern(pattern, {})
    lowered = html.lower()
    assert "empty" in lowered or "nothing" in lowered, (
        f"{pattern} has no empty state; a generated page with no data would look "
        "broken rather than merely empty")


# ---------------------------------------------------------------------------
# Relative URLs only
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template", sorted(
    p.name for p in kit_dir().glob("*.j2")), ids=sorted(p.name for p in kit_dir().glob("*.j2")))
def test_no_template_uses_an_absolute_asset_url(template: str) -> None:
    """The host strips /apps/<slug>, so "/static/..." resolves to the host root and 404s.

    Checked against the template source rather than the rendered output: an
    absolute URL inside a conditional block would not appear in every render, and
    would then 404 only in production.
    """
    text = (kit_dir() / template).read_text(encoding="utf-8")
    offenders = re.findall(r'(?:href|src|action)\s*=\s*["\'](/[^"\']*)["\']', text)
    # A protocol-relative or absolute-URL string is fine to skip here; we only
    # care about root-relative paths, which are the ones that break behind nginx.
    assert not offenders, f"{template} uses root-relative URL(s) {offenders}"


def test_the_base_layout_loads_assets_relatively() -> None:
    html = build_env().get_template("base.html.j2").render()
    assert 'href="static/tokens.css"' in html
    assert 'src="static/htmx.min.js"' in html
    assert 'href="/static' not in html


def test_no_template_loads_htmx_from_a_cdn() -> None:
    """A generated app must work offline and behind a path-stripping host."""
    for path in kit_dir().glob("*.j2"):
        text = path.read_text(encoding="utf-8").lower()
        assert "unpkg.com" not in text, path.name
        assert "cdn.jsdelivr" not in text, path.name
        assert "cdnjs.cloudflare" not in text, path.name


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------

def test_masking_extends_the_platform_key_set_rather_than_replacing_it() -> None:
    """Every key the audit agent redacts must also be masked on the page."""
    from audit_agent.contracts import REDACTED_KEYS
    missing = set(REDACTED_KEYS) - set(SENSITIVE_KEYS)
    assert not missing, f"the kit would print these: {sorted(missing)}"


@pytest.mark.parametrize("key", [
    "auth_code", "bank_account", "api_key", "password", "token", "secret",
    "card_number", "iban", "client_secret", "dashscope_api_key",
])
def test_a_credential_shaped_key_is_masked(key: str) -> None:
    """`auth_code` is NOT in the platform's REDACTED_KEYS, and it is exactly what
    a payment proposal carries — so the kit must add it, not inherit the set."""
    masked = mask_arguments({key: "super-secret-value"})
    assert masked[key] == "***REDACTED***"
    assert "super-secret-value" not in str(masked)


def test_masking_is_case_insensitive() -> None:
    assert mask_arguments({"AUTH_CODE": "x"})["AUTH_CODE"] == "***REDACTED***"
    assert mask_arguments({"Api-Key": "x"})["Api-Key"] == "***REDACTED***"


def test_masking_leaves_business_values_alone() -> None:
    """A queue token or a pagination token is legitimate data, not a credential.

    Masking is shallow for this reason: a deep walk would rewrite nested keys that
    happen to be named `token`, and silently mangling real data is its own bug.
    """
    args = {"invoice_id": "INV-1001", "amount_cents": 1250, "currency": "USD",
            "payee": "Northwind"}
    assert mask_arguments(args) == args


def test_masking_handles_none_and_empty() -> None:
    assert mask_arguments(None) == {}
    assert mask_arguments({}) == {}


def test_masking_does_not_mutate_its_input() -> None:
    original = {"auth_code": "secret", "amount_cents": 100}
    snapshot = dict(original)
    mask_arguments(original)
    assert original == snapshot


# ---------------------------------------------------------------------------
# The approval queue and the held-action card
# ---------------------------------------------------------------------------

HELD = {
    "hold_id": "hold-1",
    "request_id": "req-1",
    "app_id": "invoice-payer",
    "agent_id": "invoice-payer",
    "principal": "owner",
    "task_type": "payables.pay_invoice",
    "tool": "payments.send",
    "arguments": {"payee": "Northwind", "amount_cents": 4800000,
                  "currency": "USD", "auth_code": "abc123-secret"},
    "findings": [{"rule_id": "app.large_payment_approval", "severity": "warn",
                  "message": "payments over 25,000 need owner approval"}],
    "owner_question": "Approve a payment of 48,000.00 USD to Northwind?",
    "created_at": "2026-10-15T09:00:00+00:00",
    "status": "pending",
    "decided_by": None,
    "decided_at": None,
}


def test_the_approval_queue_renders_a_held_action() -> None:
    html = render_pattern("approval_queue", {"holds": [HELD]})
    assert "hold-1" in html
    assert "payments.send" in html
    assert "Approve a payment of 48,000.00 USD to Northwind?" in html


def test_the_held_card_shows_the_rule_that_held_it() -> None:
    """The owner has to see WHY, or the decision is a coin flip."""
    html = render_pattern("approval_queue", {"holds": [HELD]})
    assert "app.large_payment_approval" in html
    assert "payments over 25,000 need owner approval" in html


def test_the_held_card_masks_the_auth_code() -> None:
    """The single most important assertion in this file."""
    html = render_pattern("approval_queue", {"holds": [HELD]})
    assert "abc123-secret" not in html
    assert "***REDACTED***" in html
    # The business values the owner needs in order to decide must still be visible.
    assert "Northwind" in html
    assert "4800000" in html


def test_the_held_card_marks_a_masked_value_visibly() -> None:
    """A withheld value must not look like an empty one."""
    html = render_pattern("approval_queue", {"holds": [HELD]})
    assert 'class="masked"' in html


def test_the_held_card_offers_both_decisions() -> None:
    html = render_pattern("approval_queue", {"holds": [HELD]})
    assert ">Approve<" in html
    assert ">Decline<" in html


def test_the_decision_posts_the_hold_id_and_uses_htmx() -> None:
    """An approval applies only to the exact action that was held, so the id travels."""
    html = render_pattern("approval_queue", {"holds": [HELD]})
    assert 'name="hold_id"' in html
    assert 'value="hold-1"' in html
    assert "hx-post" in html
    assert 'name="decision"' in html


def test_a_decided_hold_shows_no_buttons() -> None:
    """Re-deciding a settled hold would be a second execution path."""
    decided = dict(HELD, status="approved", decided_by="owner",
                   decided_at="2026-10-15T10:00:00+00:00")
    html = render_pattern("approval_queue", {"holds": [decided]})
    assert ">Approve<" not in html
    assert ">Decline<" not in html
    assert "approved" in html


def test_a_hold_without_findings_still_renders() -> None:
    partial = dict(HELD, findings=[])
    html = render_pattern("approval_queue", {"holds": [partial]})
    assert "hold-1" in html


def test_a_hold_with_no_arguments_says_so() -> None:
    html = render_pattern("approval_queue", {"holds": [dict(HELD, arguments={})]})
    assert "carries no arguments" in html


def test_a_hold_with_a_missing_owner_question_falls_back() -> None:
    """A blank question would leave the owner with nothing to decide on."""
    html = render_pattern("approval_queue", {"holds": [dict(HELD, owner_question="")]})
    assert "needs your approval" in html


def test_the_held_card_renders_under_strict_undefined() -> None:
    """The partial must survive StrictUndefined — it is never reached by the
    pattern-level strict test, because that renders an empty queue.

    This matters more for the partial than for any other template: a hold record
    missing a field mid-write, or produced by an older runtime, must not turn the
    whole approval queue into an error page. A queue that errors hides every other
    pending action behind it, so one malformed hold would hide all of them.
    """
    env = build_env(strict=True)
    # A complete record.
    assert "hold-1" in render_pattern("approval_queue", {"holds": [HELD]}, env=env)
    # A minimal one: only the fields a caller is guaranteed to have.
    html = render_pattern("approval_queue", {"holds": [{"hold_id": "h2", "tool": "email.send"}]},
                          env=env)
    assert "h2" in html
    assert "email.send" in html
    assert ">Approve<" in html
    # And an empty one, which must still render rather than raise.
    assert "unknown" in render_pattern("approval_queue", {"holds": [{}]}, env=env)


def test_the_held_card_masks_under_strict_undefined() -> None:
    """Masking must not depend on lenient undefined behaviour."""
    env = build_env(strict=True)
    html = render_pattern("approval_queue", {"holds": [HELD]}, env=env)
    assert "abc123-secret" not in html
    assert "***REDACTED***" in html


def test_an_empty_queue_explains_itself() -> None:
    html = render_pattern("approval_queue", {"holds": []})
    assert "Nothing needs your decision" in html


def test_the_queue_can_show_what_was_already_decided() -> None:
    """The history renders alongside holds, so the owner sees what they settled."""
    decided = [{"tool": "payments.send", "status": "rejected",
                "decided_by": "owner", "decided_at": "2026-10-15T10:00:00"}]

    html = render_pattern("approval_queue", {"holds": [HELD], "decided": decided})
    assert "Decided today" in html
    assert "rejected" in html
    assert "owner" in html

    # Without any pending holds there is no queue, and the history is not the
    # point of the page — the empty state is.
    empty = render_pattern("approval_queue", {"holds": [], "decided": decided})
    assert "Nothing needs your decision" in empty


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------

def test_record_values_are_html_escaped() -> None:
    """A generated page renders record fields that came from user data.

    Without escaping, an invoice note becomes an XSS vector — and the notes are
    exactly the field an app is most likely to render unescaped, because it is the
    one that "just contains text".
    """
    html = render_pattern("detail", {"record": {"notes": "<script>alert(1)</script>"}})
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_a_table_cell_is_html_escaped() -> None:
    html = render_pattern("table", {
        "columns": [{"key": "name", "label": "Name"}],
        "rows": [{"name": "<img src=x onerror=alert(1)>"}],
    })
    assert "<img src=x" not in html
    assert "&lt;img" in html


def test_a_form_error_message_is_escaped() -> None:
    html = render_pattern("form", {
        "fields": [{"name": "amount", "label": "Amount", "type": "decimal"}],
        "form_error": "<b>bad</b>",
    })
    assert "<b>bad</b>" not in html


# ---------------------------------------------------------------------------
# The other patterns, with realistic contexts
# ---------------------------------------------------------------------------

def test_summary_renders_tones_and_values() -> None:
    html = render_pattern("summary", {"tiles": [
        {"label": "Overdue", "value": "12,500.00 USD", "tone": "danger", "hint": "3 invoices"},
        {"label": "Paid today", "value": "1,250.00 USD", "tone": "ok"},
    ]})
    assert "Overdue" in html and "12,500.00 USD" in html
    assert "tile--danger" in html and "tile--ok" in html


def test_summary_links_to_the_queue_when_actions_are_held() -> None:
    html = render_pattern("summary", {"tiles": [], "holds_count": 2})
    assert "2 actions waiting for your decision" in html


def test_summary_does_not_link_to_the_queue_when_nothing_is_held() -> None:
    html = render_pattern("summary", {"tiles": [{"label": "x", "value": "1"}], "holds_count": 0})
    assert "waiting for your decision" not in html


def test_table_renders_rows_and_a_numeric_column() -> None:
    html = render_pattern("table", {
        "columns": [{"key": "id", "label": "Invoice"},
                    {"key": "amount", "label": "Amount", "numeric": True}],
        "rows": [{"id": "INV-1", "amount": "100.00"}, {"id": "INV-2", "amount": None}],
    })
    assert "INV-1" in html and "INV-2" in html
    assert 'class="num"' in html
    # A missing value renders as an em dash, not as "None".
    assert "None" not in html


def test_table_links_the_first_column_when_a_href_is_given() -> None:
    html = render_pattern("table", {
        "columns": [{"key": "id", "label": "Invoice"}],
        "rows": [{"id": "INV-1"}],
        "row_href": "invoices/", "row_key": "id",
    })
    assert 'href="invoices/INV-1"' in html


def test_table_renders_sort_links_when_asked() -> None:
    html = render_pattern("table", {
        "columns": [{"key": "amount", "label": "Amount"}],
        "rows": [{"amount": "1.00"}],
        "sort_href": "?sort=", "sort_key": "amount", "sort_dir": "asc",
    })
    assert "?sort=amount" in html


def test_detail_renders_every_field_sorted() -> None:
    html = render_pattern("detail", {"record": {"b": 2, "a": 1}})
    assert html.index(">a<") < html.index(">b<")


def test_detail_renders_a_boolean_readably() -> None:
    html = render_pattern("detail", {"record": {"disputed": True, "paid": False}})
    assert "yes" in html and "no" in html


def test_detail_offers_edit_only_when_an_href_is_given() -> None:
    assert ">Edit<" in render_pattern("detail", {"record": {"id": 1}, "edit_href": "edit/"})
    assert ">Edit<" not in render_pattern("detail", {"record": {"id": 1}})


def test_form_renders_the_spec_field_types() -> None:
    html = render_pattern("form", {"fields": [
        {"name": "amount", "label": "Amount", "type": "decimal", "required": True},
        {"name": "days", "label": "Days", "type": "integer"},
        {"name": "due", "label": "Due", "type": "date"},
        {"name": "notes", "label": "Notes", "type": "textarea"},
        {"name": "paid", "label": "Paid", "type": "boolean"},
    ]})
    assert 'type="number"' in html and 'step="0.01"' in html
    assert 'step="1"' in html
    assert 'type="date"' in html
    assert "<textarea" in html
    assert 'type="checkbox"' in html


def test_form_marks_required_fields_and_says_the_server_rechecks() -> None:
    html = render_pattern("form", {"fields": [
        {"name": "amount", "type": "decimal", "required": True}]})
    assert "required" in html
    assert "checked again on the server" in html


def test_form_shows_a_per_field_error_and_keeps_the_typed_value() -> None:
    """A failed submit that loses what the user typed makes them retype it."""
    html = render_pattern("form", {
        "fields": [{"name": "amount", "label": "Amount", "type": "decimal",
                    "value": "12.50"}],
        "field_errors": {"amount": "must be positive"},
    })
    assert 'value="12.50"' in html
    assert "must be positive" in html
    assert 'aria-invalid="true"' in html


def test_form_renders_a_select_from_options() -> None:
    html = render_pattern("form", {"fields": [
        {"name": "currency", "label": "Currency", "options": ["USD", "EUR"],
         "value": "EUR"}]})
    assert "<select" in html
    assert 'value="EUR" selected' in html


def test_settings_separates_what_the_owner_may_change_from_what_they_may_not() -> None:
    """A settings page that lets an owner raise a ceiling lets them weaken a control."""
    html = render_pattern("settings", {
        "limits": [{"name": "max_payment", "label": "Max payment", "type": "decimal",
                    "value": "25000.00"}],
        "policy": [{"id": "app.large_payment_approval", "tools": ["payments.send"],
                    "message": "payments over 25,000 need owner approval"}],
    })
    assert 'name="max_payment"' in html
    assert "app.large_payment_approval" in html
    assert "Read-only" in html
    # The rule id must not appear inside an editable input.
    assert '<input type="text" name="app.large_payment_approval"' not in html


def test_settings_with_no_editable_limits_says_so() -> None:
    html = render_pattern("settings", {"limits": [], "policy": [
        {"id": "app.x", "tools": [], "message": "m"}]})
    assert "No owner-settable limits" in html


# ---------------------------------------------------------------------------
# The base layout
# ---------------------------------------------------------------------------

def test_the_layout_renders_navigation_from_the_pages_context() -> None:
    html = build_env().get_template("base.html.j2").render(
        app_name="Invoice Payer",
        pages=[{"id": "dashboard", "title": "Overview", "href": "./", "current": True},
               {"id": "holds", "title": "Approvals", "href": "holds/"}])
    assert "Invoice Payer" in html
    assert 'href="holds/"' in html
    assert 'aria-current="page"' in html


def test_the_layout_renders_a_page_level_error() -> None:
    html = build_env().get_template("base.html.j2").render(error="the database is locked")
    assert 'role="alert"' in html
    assert "the database is locked" in html


def test_the_layout_declares_a_viewport_and_colour_scheme() -> None:
    html = build_env().get_template("base.html.j2").render()
    assert 'name="viewport"' in html
    assert 'name="color-scheme"' in html


def test_the_layout_is_valid_html_with_a_single_root() -> None:
    """Rough structural check: a generated page must not be a fragment."""
    for pattern in sorted(PATTERNS):
        html = render_pattern(pattern, {})
        assert html.count("<!doctype html>") == 1, pattern
        assert html.count("</html>") == 1, pattern
        assert html.count("<html") == 1, pattern


# ---------------------------------------------------------------------------
# A generated app's own templates directory
# ---------------------------------------------------------------------------

def test_the_env_can_be_pointed_at_an_app_template_directory(tmp_path: Path) -> None:
    """The scaffolder copies kit templates into the app; the app then loads its own."""
    (tmp_path / "summary.html.j2").write_text(
        '{% extends "base.html.j2" %}{% block content %}<p>app copy</p>{% endblock %}',
        encoding="utf-8")
    import shutil
    shutil.copy(kit_dir() / "base.html.j2", tmp_path / "base.html.j2")

    html = render_pattern("summary", {}, searchpath=tmp_path)
    assert "app copy" in html


def test_a_missing_template_raises_rather_than_rendering_nothing(tmp_path: Path) -> None:
    with pytest.raises(TemplateNotFound):
        build_env(tmp_path).get_template("summary.html.j2")


# ---------------------------------------------------------------------------
# The real apps' declared patterns are all covered
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("app", [
    REPO / "apps" / "helios-collect",
    REPO / "entry-point" / "examples" / "invoice-payer",
], ids=["helios-collect", "invoice-payer"])
def test_every_pattern_the_real_apps_declare_is_available(app: Path) -> None:
    spec = AppSpec.load(app / "helios-app.yaml")
    declared = {str(p.pattern) for p in spec.pages}
    assert missing_patterns(declared) == []
    for pattern in declared:
        assert render_pattern(pattern, {}).strip()

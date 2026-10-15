"""The UI kit: Jinja2 environment, design tokens and the six page patterns.

`specs/app-builder.md` describes this as a React component library built with
Vite. It is Jinja2 + HTMX instead, and the reason is recorded in the spec:
a generated app has to be buildable and debuggable by a model in minutes, and a
Node toolchain is neither. Server-rendered pages mean one process, one port, one
origin — the same property the spec wanted from Vite, reached without a build step.

Three rules the templates follow:

1. **Relative URLs only.** The deployment host strips `/apps/<slug>` before the
   request reaches the app, so an absolute `/static/tokens.css` resolves to the
   host root and 404s. Every `href` and `src` here is relative. The consequence is
   that generated pages must be served at flat routes (`/`, `/holds`, `/runs`),
   not nested ones — a page at `/holds/123` would resolve `static/...` against
   `/holds/`. `server.py` honours that.
2. **Every template renders with an empty context.** A generated app that renders
   "undefined" in an empty table reads as broken however good the rest of it
   looks, so loading, empty and error states are built into the patterns rather
   than left to the caller.
3. **Tokens, not colours.** Restyling an app — including adopting an existing
   system's look via `ui.align_with` — means changing `tokens.css`, not the
   templates. Light and dark come from `prefers-color-scheme` on the same tokens.

Masking held-action arguments
-----------------------------
The audit agent already redacts `HoldRecord.arguments` at the model boundary, but
the approval queue is the one page where a mistake puts a credential on a screen
in front of a human. So `mask_arguments` applies its own filter too, and it
*extends* the platform key set rather than replacing it: `auth_code`,
`bank_account` and similar are not in `REDACTED_KEYS`, and they are exactly what
a payment proposal carries.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from jinja2 import Environment, FileSystemLoader, StrictUndefined, Undefined, select_autoescape

# This module lives at app_builder/kit/__init__.py, next to the templates it
# loads, so the sibling modules are one level up.
from ..stacks import kit_dir
from ..spec import UI_PATTERNS

#: The templates and static assets shipped with the kit.
KIT_DIR = Path(__file__).resolve().parent

__all__ = [
    "PATTERNS", "SENSITIVE_KEYS", "build_env", "kit_dir", "mask_arguments",
    "template_for_pattern", "render_pattern", "missing_patterns",
    "validate_pattern_set", "HELD_ACTION_PARTIAL",
]

#: Pattern name -> template filename. A pattern with no template would generate a
#: page that renders nothing, so `validate` and the scaffolder both check this map
#: against the spec's declared patterns.
PATTERNS: dict[str, str] = {
    "summary": "summary.html.j2",
    "table": "table.html.j2",
    "detail": "detail.html.j2",
    "form": "form.html.j2",
    "approval_queue": "approval_queue.html.j2",
    "settings": "settings.html.j2",
}

#: Held-action partial, included by approval_queue.
HELD_ACTION_PARTIAL = "_held_action.html.j2"

#: The platform's credential keys, EXTENDED with what a financial proposal
#: actually carries. `auth_code` is a single-use credential bound to
#: (amount, currency, payee) and is not in `audit_agent.contracts.REDACTED_KEYS`,
#: so relying on the platform set alone would print it to the approval queue.
_SENSITIVE_EXTRA = frozenset({
    "auth_code", "authcode", "auth-code", "intent_code", "verification_code",
    "otp", "pin", "cvv", "cvc", "card_number", "cardnumber", "pan",
    "bank_account", "bankaccount", "account_number", "accountnumber",
    "iban", "swift", "routing_number", "sort_code", "ssn",
})


def _platform_keys() -> frozenset[str]:
    """The audit agent's REDACTED_KEYS, or an empty set if it is not installed.

    Imported lazily and defensively: the kit must be usable (and testable) without
    the sibling packages present, and silently falling back to the extra keys only
    would lose the platform's list.
    """
    try:
        from audit_agent.contracts import REDACTED_KEYS
    except Exception:
        return frozenset()
    return frozenset(REDACTED_KEYS)


SENSITIVE_KEYS: frozenset[str] = _platform_keys() | _SENSITIVE_EXTRA


def mask_arguments(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """A copy of `arguments` with sensitive keys masked, one level deep.

    Shallow on purpose, matching `audit_agent.contracts.redact`: a deep walk would
    rewrite a nested key that happens to be named `token` for a legitimate
    business reason (a queue token, a pagination token), and silently mangling
    real data is its own kind of bug.
    """
    if not arguments:
        return {}
    out: dict[str, Any] = {}
    for key, value in arguments.items():
        out[key] = "***REDACTED***" if str(key).lower() in SENSITIVE_KEYS else value
    return out


def build_env(searchpath: Path | str | None = None,
              *, strict: bool = False) -> Environment:
    """A Jinja2 environment for the kit, or for a generated app's templates.

    `strict=True` makes an undefined variable raise instead of rendering as an
    empty string. Generated apps should use it during development: a typo'd
    variable in a template is otherwise invisible until someone notices a blank
    cell. The kit's own tests render both ways — strict, to catch typos, and
    lenient, because every pattern must also tolerate a minimal context.

    Autoescaping is on for HTML. A generated page renders record fields that came
    from user data; without escaping, an invoice note becomes an XSS vector.
    """
    path = Path(searchpath) if searchpath is not None else kit_dir()
    env = Environment(
        loader=FileSystemLoader(str(path)),
        autoescape=select_autoescape(("html", "xml", "j2", "html.j2"), default=True),
        # `undefined` must be an Undefined subclass. Passing None fails Jinja2's
        # own environment sanity check ("'undefined' must be a subclass of
        # jinja2.Undefined"), so the lenient default is the base class.
        undefined=StrictUndefined if strict else Undefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["mask"] = mask_arguments
    env.globals["SENSITIVE_KEYS"] = sorted(SENSITIVE_KEYS)
    return env


def template_for_pattern(pattern: str) -> str:
    """The template filename for a spec pattern.

    Raises `KeyError` rather than falling back to a default: rendering the wrong
    pattern silently would produce a page that looks plausible and shows the wrong
    thing, which is worse than an error.
    """
    if pattern not in PATTERNS:
        raise KeyError(
            f"{pattern!r} is not a known ui pattern; write one of "
            f"{sorted(PATTERNS)}. Each pattern names a template in the kit, so an "
            "unknown one has nothing to render."
        )
    return PATTERNS[pattern]


def render_pattern(pattern: str, context: Mapping[str, Any] | None = None,
                   *, env: Environment | None = None,
                   searchpath: Path | str | None = None) -> str:
    """Render one pattern with a context. Convenience wrapper for apps and tests."""
    environment = env or build_env(searchpath)
    return environment.get_template(template_for_pattern(pattern)).render(**(context or {}))


def missing_patterns(declared: Iterable[str]) -> list[str]:
    """Declared patterns with no template in the kit — checked at generation time."""
    available = {p.name for p in kit_dir().glob("*.j2")}
    return sorted(
        name for name in declared
        if name in PATTERNS and PATTERNS[name] not in available
    )


def validate_pattern_set() -> None:
    """Fail loudly if the kit and the spec's closed set have drifted.

    The spec model defines `UI_PATTERNS`; this module maps pattern -> template. If
    a pattern is added to one and not the other, every app declaring it would
    generate a page that does not exist. That is a startup check, not a test-only
    assertion, because it is cheap and the failure it prevents is confusing.
    """
    missing = sorted(set(UI_PATTERNS) - set(PATTERNS))
    if missing:
        raise RuntimeError(
            f"the UI kit has no template mapping for pattern(s) {missing}; add them "
            "to app_builder.kit.PATTERNS and ship the template")
    extra = sorted(set(PATTERNS) - set(UI_PATTERNS))
    if extra:
        raise RuntimeError(
            f"the UI kit maps pattern(s) {extra} which the spec model does not "
            "allow; add them to app_builder.spec.UiPattern or remove the template")

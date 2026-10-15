"""Boot a scaffolded app and make real HTTP requests to it.

This file exists because of a specific failure. The scaffolder emitted
`app.mount("static", ...)` — Starlette requires a mount path to start with "/", so
the generated `server.py` raised `AssertionError: Routed paths must start with '/'`
at import time. Every generated application would have failed the deployment
engine's smoke test instantly.

It went unnoticed through 386 passing tests, because every other test treated
generated output as TEXT: it grepped for strings, or compiled a module. None of
them imported the module into a running app and issued a request. A test suite
that only reads generated code cannot tell you whether that code runs.

So these tests do the thing nothing else did — they import the generated server,
put it in Starlette's TestClient, and request the routes. In-process: no socket, no
background server, no port that a parallel test run might already hold.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from app_builder.scaffold import scaffold
from app_builder.spec import AppSpec

REPO = Path(__file__).resolve().parents[3]
INVOICE_PAYER = REPO / "entry-point" / "examples" / "invoice-payer"
HELIOS_COLLECT = REPO / "apps" / "helios-collect"

BOTH_APPS = pytest.mark.parametrize(
    "spec_path", [INVOICE_PAYER / "helios-app.yaml", HELIOS_COLLECT / "helios-app.yaml"],
    ids=["invoice-payer", "helios-collect"],
)


@pytest.fixture()
def scaffolded(tmp_path: Path):
    """A freshly scaffolded app on sys.path, yielded as (dir, imported server module).

    sys.path is restored on the way out. Without that, each test would leave the
    generated `app` and `store` modules importable by the NEXT test, and a module
    named `app` colliding between two scaffolded apps produces failures that point
    at the wrong file.
    """
    created: list[Path] = []

    def _make(spec_path: Path, subdir: str = "proto"):
        target = tmp_path / subdir
        spec = AppSpec.load(spec_path)
        # scaffold raises ScaffoldError on failure rather than returning errors,
        # so there is nothing to assert on here beyond "it produced files".
        result = scaffold(target, spec, spec_path=spec_path)
        assert result.written, f"scaffolding {spec_path} wrote nothing"
        created.append(target)
        return target

    saved_path = list(sys.path)
    saved_modules = {
        name for name in sys.modules
        if name in ("app", "server", "store", "facts", "tools", "graph")
    }
    try:
        yield _make
    finally:
        sys.path[:] = saved_path
        for name in list(sys.modules):
            if name in ("app", "server", "store", "facts", "tools", "graph", "config"):
                if name not in saved_modules:
                    del sys.modules[name]
        for target in created:
            # Only this test's own scaffolding, inside tmp_path.
            assert str(target).startswith(str(tmp_path))


def _import_module(directory: Path, module_name: str):
    """Import a generated module with `directory` first on sys.path."""
    path = str(directory)
    if path not in sys.path:
        sys.path.insert(0, path)
    for stale in ("app", "server", "store", "facts", "tools", "graph"):
        sys.modules.pop(stale, None)
    import importlib

    return importlib.import_module(module_name)


# ---------------------------------------------------------------------------
# Every generated module must at least be valid Python
# ---------------------------------------------------------------------------

@BOTH_APPS
def test_every_generated_python_module_parses(scaffolded, spec_path):
    """Parse each generated .py file. Catches a template emitting bad syntax."""
    app_dir = scaffolded(spec_path)
    parsed = 0
    for path in sorted(app_dir.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        try:
            ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            pytest.fail(f"{path.relative_to(app_dir)} has a syntax error: {exc}")
        parsed += 1
    # app.py, server.py, store.py, facts.py, tools.py, graph.py, tests/…
    assert parsed >= 6, f"only {parsed} generated modules; expected at least 6"


# ---------------------------------------------------------------------------
# The generated server must IMPORT — the bug this file exists for
# ---------------------------------------------------------------------------

@BOTH_APPS
def test_the_generated_server_imports(scaffolded, spec_path):
    """The regression test. A mount path missing its leading '/' fails HERE."""
    app_dir = scaffolded(spec_path)
    try:
        server = _import_module(app_dir, "server")
    except Exception as exc:  # noqa: BLE001 - the point is that ANY error is a fail
        pytest.fail(
            f"the generated server.py does not import: {type(exc).__name__}: {exc}. "
            "The deployment engine smoke-tests `entry: server:app`, so a module "
            "that raises at import time takes the whole app offline."
        )
    assert hasattr(server, "app"), "server.py must expose an ASGI app named `app`"


@BOTH_APPS
def test_every_routed_path_starts_with_a_slash(scaffolded, spec_path):
    """Assert Starlette's own precondition directly, with a message that names it.

    The import test above already catches this, but as an AssertionError from deep
    inside Starlette. Naming the invariant here means a future change that breaks
    it fails with a sentence a model can act on.
    """
    app_dir = scaffolded(spec_path)
    server = _import_module(app_dir, "server")
    bad = [
        getattr(route, "path", None)
        for route in server.app.routes
        if getattr(route, "path", None) is not None
        and not str(route.path).startswith("/")
    ]
    assert not bad, (
        f"routed paths must start with '/'; got {bad}. A route or mount declared "
        "without the leading slash makes Starlette reject the whole app at import."
    )


# ---------------------------------------------------------------------------
# The routes must answer
# ---------------------------------------------------------------------------

@BOTH_APPS
def test_health_answers_without_touching_the_database(scaffolded, spec_path):
    """/health is the deployment contract. It must not need the app built.

    A health check that fails because a fixture is missing takes the container
    offline, which is worse than reporting that the process is up.
    """
    from starlette.testclient import TestClient

    app_dir = scaffolded(spec_path)
    server = _import_module(app_dir, "server")
    with TestClient(server.app) as client:
        response = client.get("/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["app_id"] == AppSpec.load(spec_path).app_id


@BOTH_APPS
def test_the_dashboard_renders_html(scaffolded, spec_path):
    from starlette.testclient import TestClient

    app_dir = scaffolded(spec_path)
    server = _import_module(app_dir, "server")
    with TestClient(server.app) as client:
        response = client.get("/")
    assert response.status_code == 200, response.text
    assert "<!doctype html>" in response.text.lower()
    assert "undefined" not in response.text.lower(), (
        "a template rendered an undefined variable; the kit requires every "
        "pattern to render with a minimal context"
    )


@BOTH_APPS
def test_static_assets_are_served(scaffolded, spec_path):
    """The mount is what broke, so assert the assets actually come back."""
    from starlette.testclient import TestClient

    app_dir = scaffolded(spec_path)
    server = _import_module(app_dir, "server")
    with TestClient(server.app) as client:
        css = client.get("/static/tokens.css")
        htmx = client.get("/static/htmx.min.js")
    assert css.status_code == 200, f"tokens.css: {css.status_code}"
    assert "text/css" in css.headers.get("content-type", "")
    assert htmx.status_code == 200, f"htmx.min.js: {htmx.status_code}"
    assert len(htmx.content) > 10_000, "the vendored htmx looks truncated"


# ---------------------------------------------------------------------------
# The approval queue must actually decide — not raise, not silently 200
# ---------------------------------------------------------------------------

@BOTH_APPS
def test_deciding_without_a_hold_id_is_a_named_400(scaffolded, spec_path):
    from starlette.testclient import TestClient

    app_dir = scaffolded(spec_path)
    server = _import_module(app_dir, "server")
    with TestClient(server.app, raise_server_exceptions=False) as client:
        response = client.post("/holds/decide")
    assert response.status_code == 400
    assert "hold_id" in response.text


@BOTH_APPS
def test_an_invalid_decision_value_names_the_accepted_ones(scaffolded, spec_path):
    from starlette.testclient import TestClient

    app_dir = scaffolded(spec_path)
    server = _import_module(app_dir, "server")
    with TestClient(server.app, raise_server_exceptions=False) as client:
        response = client.post("/holds/decide", content="hold_id=x&decision=maybe")
    assert response.status_code == 400
    assert "approve" in response.text and "decline" in response.text
    assert "maybe" in response.text, "the reply should echo what was received"


@BOTH_APPS
def test_an_unknown_hold_reports_conflict_with_the_auditors_message(
    scaffolded, spec_path
):
    """A 409, not a 200.

    This is the security-shaped assertion. An endpoint that returns success for a
    hold it did not decide leaves the owner believing they approved an action that
    never ran — or is about to run unapproved. Failure must be visible.
    """
    from starlette.testclient import TestClient

    app_dir = scaffolded(spec_path)
    server = _import_module(app_dir, "server")
    with TestClient(server.app, raise_server_exceptions=False) as client:
        response = client.post(
            "/holds/decide", content="hold_id=not-a-real-hold&decision=approve"
        )
    assert response.status_code == 409, (
        f"expected 409 for an unknown hold, got {response.status_code}: "
        f"{response.text[:200]}"
    )
    assert "not-a-real-hold" in response.text


@BOTH_APPS
def test_the_decide_endpoint_does_not_need_python_multipart(scaffolded, spec_path):
    """Guards the reason the endpoint parses its body by hand.

    Starlette asserts `python-multipart` is installed before parsing ANY form —
    including urlencoded ones. That package is not a Helios dependency, so
    `await request.form()` would fail a generated app's first approval POST with an
    AssertionError naming a package the app never asked for.
    """
    app_dir = scaffolded(spec_path)
    tree = ast.parse((app_dir / "server.py").read_text(encoding="utf-8"),
                     filename="server.py")
    calls = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "form"
    ]
    # AST rather than a text search: the generated `_parse_qs` docstring explains
    # why request.form() is avoided, and that explanation contains the very string
    # a grep would match. A test that fails on its own subject's commentary is
    # testing nothing.
    assert not calls, (
        f"server.py calls {calls}: request.form() requires python-multipart, which "
        "is not a Helios dependency. Parse the body with urllib.parse instead."
    )
    source = ast.unparse(tree)
    assert "parse_qs" in source


def test_the_generated_server_has_no_unimplemented_route_handlers(tmp_path):
    """No route may be a stub that raises.

    The decide endpoint used to raise NotImplementedError. Raising is better than a
    silent 200, but a prototype with a route that cannot answer is not a prototype.
    This test keeps it that way: if a route is added before it is wired, it must
    either work or be absent, not raise on request.
    """
    target = tmp_path / "no-stubs"
    spec = AppSpec.load(INVOICE_PAYER / "helios-app.yaml")
    scaffold(target, spec, spec_path=INVOICE_PAYER / "helios-app.yaml")

    source = (target / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="server.py")
    decorated: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list:
            for decorator in node.decorator_list:
                text = ast.unparse(decorator)
                if text.startswith("app."):
                    decorated.append(node.name)
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Raise):
                            raised = ast.unparse(sub)
                            pytest.fail(
                                f"route handler {node.name} raises: {raised}. "
                                "Wire it, or do not declare the route."
                            )
    assert decorated, "no route handlers found; the decorator scan is broken"

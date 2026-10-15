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


# ---------------------------------------------------------------------------
# The scenario runner must actually drive the gate
# ---------------------------------------------------------------------------

def test_the_generated_app_runs_a_scenario_through_the_gate(tmp_path):
    """End to end: build the app, run a scenario, get a verdict from the auditor.

    This is the strongest statement the scaffolder can make about its output —
    not "the text looks right" but "a side effect went through the audit gate and
    came back with a decision". It uses the invoice-payer spec because that app's
    manifests are complete enough to load and grant.

    Tool handlers are generated as TODO stubs that RAISE, so this test supplies a
    real one in-process. That is deliberate and mirrors what an author does next:
    the scaffolder must not invent business logic, and a stub that returned an
    empty result would let a demo succeed while doing nothing.
    """
    target = tmp_path / "runner"
    spec_path = INVOICE_PAYER / "helios-app.yaml"
    spec = AppSpec.load(spec_path)
    scaffold(target, spec, spec_path=spec_path)

    sys_path = str(target)
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    for stale in ("app", "server", "store", "facts", "tools", "graph"):
        sys.modules.pop(stale, None)

    import importlib

    import yaml

    app_mod = importlib.import_module("app")

    # The audit manifest declares this task; a scenario must name a task the
    # auditor has policy for, or the grant fails closed with "no policy registered".
    audit = yaml.safe_load(
        (target / "agents" / f"{spec.app_id}-audit.yaml").read_text("utf-8")
    )
    task_type = next(iter(audit["tasks"]))

    built = app_mod.build_app(run_dir=target / "runs")
    try:
        executed: list[dict] = []

        def real_handler(args):
            executed.append(dict(args))
            return {"ok": True}

        gated = None
        for definition in built.integration.tools:
            if definition.side_effect:
                gated = definition.name
                object.__setattr__(definition, "handler", real_handler)
        assert gated, "the generated app should declare at least one gated tool"

        # Discover one argument the gated tool's schema accepts, so the call passes
        # schema validation. additionalProperties is False, so inventing a name
        # would be refused for the wrong reason and prove nothing.
        schema = next(d for d in built.integration.tools if d.name == gated).input_schema
        first_prop = next(iter(schema.get("properties", {})), None)
        arguments = {first_prop: "seeded-value"} if first_prop else {}

        scenario = app_mod.Scenario(
            subject_id="SUBJ-1", task_type=task_type,
            calls=((gated, arguments),),
            control="a gated side effect under the audit agent",
            expects="any",
        )
        owner = app_mod.ScriptedOwner(verbose=False)
        outcome = app_mod.run_scenario(built, scenario, owner)

        assert outcome.status, "the runner produced no status"
        assert outcome.status != "unknown"
        # The decisive assertion: the gate made a decision about the side effect.
        # The vocabulary here is the RUNTIME's — approve/reject/hold, as written
        # into actions[].verdict — not the auditor's Decision enum (allow/deny).
        # Two vocabularies at two layers; asserting the wrong one fails on a
        # correct app.
        verdicts = [
            (a.get("verdict") or {}).get("decision")
            for a in outcome.actions
            if a.get("verdict")
        ]
        assert verdicts, (
            f"no verdict was recorded for {gated}; actions={outcome.actions}. "
            "The runner must drive the call through the audit gate."
        )
        assert all(v in ("approve", "reject", "hold") for v in verdicts), verdicts
    finally:
        built.close()


def test_the_generated_runner_reports_a_refusal_as_not_done(tmp_path):
    """A refusal must never be summarised as success.

    `_summarize` derives the outcome from the recorded actions, not from the loop's
    status, because a run whose only side effect was REFUSED still reports
    COMPLETED — a denial is fed back to the model as an observation and the loop
    finishes normally. Reporting the loop's status would call a blocked payment a
    success, which is the single most misleading thing a demo could do.
    """
    target = tmp_path / "summarize"
    spec_path = INVOICE_PAYER / "helios-app.yaml"
    scaffold(target, AppSpec.load(spec_path), spec_path=spec_path)

    path = str(target)
    if path not in sys.path:
        sys.path.insert(0, path)
    for stale in ("app", "server", "store", "facts", "tools", "graph"):
        sys.modules.pop(stale, None)
    import importlib

    app_mod = importlib.import_module("app")

    refused = app_mod.Outcome(
        subject_id="X", task_type="t", control="c", expects="e",
        actions=[{"tool": "payments.send",
                  "verdict": {"decision": "reject"}, "executed": None}],
    )
    assert app_mod._summarize(refused, fallback="completed") == "refused"

    approved_and_run = app_mod.Outcome(
        subject_id="X", task_type="t", control="c", expects="e",
        actions=[{"tool": "payments.send",
                  "verdict": {"decision": "allow"}, "executed": {"ok": True}}],
    )
    assert app_mod._summarize(approved_and_run, fallback="completed") == "done"

    # A decline by the owner is distinguishable from a refusal by a rule.
    declined = app_mod.Outcome(
        subject_id="X", task_type="t", control="c", expects="e",
        actions=[{"tool": "payments.send",
                  "verdict": {"decision": "reject"}, "executed": None}],
        owner_decisions=[{"hold_id": "h1", "approved": False}],
    )
    assert app_mod._summarize(declined, fallback="completed") == "declined_by_owner"


def test_the_generated_app_has_no_todo_in_its_main_loop(tmp_path):
    """`python app.py` must do something, not print a TODO.

    The runner used to print "TODO: drive the runtime over scenario.calls" and
    return 0, so a generated app exited successfully having demonstrated nothing.
    """
    target = tmp_path / "no-todo-main"
    scaffold(target, AppSpec.load(INVOICE_PAYER / "helios-app.yaml"),
             spec_path=INVOICE_PAYER / "helios-app.yaml")
    source = (target / "app.py").read_text(encoding="utf-8")
    assert "TODO: drive the runtime" not in source
    for name in ("run_scenario", "_summarize", "render_dashboard"):
        assert f"def {name}" in source, f"app.py is missing {name}"
    assert "class ScriptedDriver" in source, "app.py is missing ScriptedDriver"


def test_the_generated_outcome_has_one_expectation_field(tmp_path):
    """`Outcome` must not carry two fields for the same idea.

    It once had both `expects` (set from the Scenario) and `decided` (never set,
    so always "unknown"), and main() compared the run's status against `decided`.
    The mismatch check then reported "got done, expected unknown" and only exited
    non-zero by accident — the comparison could never succeed for any declared
    expectation. One field, one meaning: `expects`.
    """
    target = tmp_path / "one-expectation"
    scaffold(target, AppSpec.load(INVOICE_PAYER / "helios-app.yaml"),
             spec_path=INVOICE_PAYER / "helios-app.yaml")
    source = (target / "app.py").read_text(encoding="utf-8")
    assert "decided: str" not in source, (
        "Outcome still declares a `decided` field alongside `expects`; the runner "
        "never sets it, so the expectation check compares against 'unknown'."
    )
    assert "o.expects != \"any\"" in source or "o.expects != 'any'" in source, (
        "main() must gate on Scenario.expects, not on an unset field"
    )


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

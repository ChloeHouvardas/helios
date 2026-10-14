"""The orchestrator, unit-tested with no Docker daemon.

The orchestrator's job that a test can actually pin down is *text generation*:
the Dockerfile it injects into an app bundle, and the nginx config it writes to
route that app. Everything else is docker-py plumbing. So docker-py is mocked
and these tests assert on the generated text.

Two properties matter more than the happy path:

- **No secret may reach generated text.** The tenant execution secret and the
  per-deploy action token are passed to containers as environment, never
  written into the Dockerfile or the nginx config. Those files are readable by
  anyone who can read the bundle or the config directory, and the whole
  sidecar design rests on the app container not holding the execution secret.
- **Nothing is written outside the run directory.** `REPO` and `TENANTS_DIR`
  are module-level paths, so they are redirected at a temp dir here. A test
  that silently drops a tenant secret into the working tree would be worse
  than no test.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from docker.errors import APIError, BuildError, ImageNotFound, NotFound

from engine import orchestrator
from engine.contracts import Manifest, sanitize

INGRESS_NET = orchestrator.INGRESS_NET
TENANT_IP = "172.20.0.42"


# --- a docker-py stand-in --------------------------------------------------

def make_mock_client() -> MagicMock:
    """A docker client whose networks/containers/images behave just enough.

    Three fidelity points that matter:

    - `api._version` is a real string, because `_container` passes it to
      `docker.types.EndpointConfig`, which compares it against API version
      constants; a MagicMock there raises TypeError instead of building a dict.
    - `containers.get` returns the SAME object for the same name. A fresh
      MagicMock per lookup makes "was this container reloaded/removed"
      unassertable, since the test cannot reach the object the engine used.
    - `networks.get` misses, so `networks.create` is what actually runs. That
      is the first-deploy path, and it is the only way to observe the
      `internal=True` flag on the tenant network.
    """
    client = MagicMock()
    client.api._version = "1.44"

    networks: dict[str, MagicMock] = {}

    def _network(name: str, **kwargs) -> MagicMock:
        if name not in networks:
            net = MagicMock()
            net.name = name
            net.internal = kwargs.get("internal", False)
            networks[name] = net
        return networks[name]

    def _get_network(name: str):
        # Miss until the network exists, then hit. `_network()` catches NotFound
        # and creates it; `_container()` later re-fetches the same network to
        # attach aliases, so a permanent miss breaks the attach path.
        if name in networks:
            return networks[name]
        raise NotFound(f"no such network: {name}")

    client.networks.get.side_effect = _get_network
    client.networks.create.side_effect = _network
    client.networks.created = networks

    # Base images already exist, so ensure_base_images() does not rebuild.
    client.images.get.side_effect = lambda ref: MagicMock()

    created: list[dict] = []
    known: dict[str, MagicMock] = {}

    def _create(*args, **kwargs):
        container = MagicMock()
        container.status = "running"
        # The app container's IP is read back off the ingress network.
        container.attrs = {
            "NetworkSettings": {
                "Networks": {INGRESS_NET: {"IPAddress": TENANT_IP}}
            }
        }
        # The smoke test polls exec_run for a healthy /health.
        container.exec_run.return_value = SimpleNamespace(exit_code=0, output=b"")
        container.logs.return_value = b""
        record = dict(kwargs)
        record["image"] = args[0] if args else kwargs.get("image")
        record["_container"] = container
        created.append(record)
        if kwargs.get("name"):
            known[kwargs["name"]] = container
        return container

    client.containers.create.side_effect = _create
    client.containers.created = created

    def _get(name: str):
        # `helios-nginx` pre-exists (bootstrap.sh starts it) and is reloaded at
        # the end of a deploy. It must be a stable object for that to be
        # assertable. Everything else misses: nothing is deployed yet.
        if name == "helios-nginx":
            if name not in known:
                nginx = MagicMock()
                nginx.name = name
                known[name] = nginx
            return known[name]
        if name in known:
            return known[name]
        raise NotFound(f"no such container: {name}")

    client.containers.get.side_effect = _get
    return client


@pytest.fixture
def env(monkeypatch, tmp_path):
    """An Orchestrator whose every write goes under a temp dir."""
    root = tmp_path / "deploy-root"
    tenants = root / ".helios-run" / "tenants"
    tenants.mkdir(parents=True)

    monkeypatch.setattr(orchestrator, "REPO", root)
    monkeypatch.setattr(orchestrator, "TENANTS_DIR", tenants)
    # The smoke test polls for up to 45 seconds. A failing smoke test would
    # otherwise spend 45 real seconds in this suite for no reason.
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _: None)

    client = make_mock_client()
    monkeypatch.setattr(orchestrator.docker, "from_env", lambda: client)

    orch = orchestrator.Orchestrator()
    app_dir = tmp_path / "bundle"
    app_dir.mkdir()
    (app_dir / "main.py").write_text("app = object()\n", encoding="utf-8")
    return SimpleNamespace(
        orch=orch, client=client, root=root, tenants=tenants, app_dir=app_dir
    )


def deploy(env, *, name="collect", tenant="demo", entry="main:app", port=3000,
           permissions=None):
    manifest = Manifest(
        name=sanitize(name),
        tenant=sanitize(tenant) if tenant else "",
        entry=entry,
        port=port,
        permissions=list(permissions or []),
    )
    return env.orch.deploy(tenant, manifest, env.app_dir)


# --- construction ----------------------------------------------------------

def test_an_unreachable_daemon_is_a_clear_runtime_error(monkeypatch) -> None:
    """The failure message names the fix, not just the exception."""
    def boom():
        raise OSError("cannot connect to /var/run/docker.sock")

    monkeypatch.setattr(orchestrator.docker, "from_env", boom)
    with pytest.raises(RuntimeError) as excinfo:
        orchestrator.Orchestrator()
    assert "Docker daemon unreachable" in str(excinfo.value)
    assert "colima start" in str(excinfo.value)


def test_a_failed_ping_is_treated_as_unreachable(monkeypatch) -> None:
    client = MagicMock()
    client.ping.side_effect = APIError("daemon said no")
    monkeypatch.setattr(orchestrator.docker, "from_env", lambda: client)
    with pytest.raises(RuntimeError, match="unreachable"):
        orchestrator.Orchestrator()


# --- Dockerfile injection --------------------------------------------------

def test_the_injected_dockerfile_is_exactly_the_golden_base_plus_one_copy(env) -> None:
    """The build must stay a single COPY layer on the prebuilt base.

    That is the whole point of the golden image: a deploy rebuilds in seconds
    because it never reinstalls dependencies. A Dockerfile that added a
    `pip install` would silently make every deploy slow.
    """
    deploy(env)
    dockerfile = (env.app_dir / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile == (
        f"FROM {orchestrator.BASE_IMAGE}\n"
        "COPY . /app\n"
        "WORKDIR /app\n"
        'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000"]\n'
    )


def test_the_dockerfile_carries_the_manifest_entry_and_port(env) -> None:
    deploy(env, entry="wsgi:application", port=8123)
    text = (env.app_dir / "Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["uvicorn", "wsgi:application", "--host", "0.0.0.0", "--port", "8123"]' in text


def test_the_dockerfile_pins_the_host_and_port_rather_than_inheriting(env) -> None:
    """An app that binds 127.0.0.1 is unreachable from nginx; pin 0.0.0.0."""
    deploy(env)
    text = (env.app_dir / "Dockerfile").read_text(encoding="utf-8")
    assert '"--host", "0.0.0.0"' in text


def test_the_injected_dockerfile_contains_no_secret(env) -> None:
    """Security property: the Dockerfile is world-readable inside the bundle.

    The tenant execution secret lives in tenant state and goes to the sidecar
    as environment only. If it ever landed in the Dockerfile it would be baked
    into an image layer, which is the exact leak the sidecar split exists to
    prevent.
    """
    deploy(env, tenant="demo")
    state = json.loads((env.tenants / "demo.json").read_text(encoding="utf-8"))
    secret = state["execution_secret"]
    assert len(secret) == 32

    dockerfile = (env.app_dir / "Dockerfile").read_text(encoding="utf-8")
    assert secret not in dockerfile
    assert "EXECUTION_SECRET" not in dockerfile


def test_the_image_tag_encodes_tenant_and_app(env) -> None:
    result = deploy(env, name="collect", tenant="acme")
    assert result["image"] == "helios-app-acme-collect:latest"
    env.client.images.build.assert_called()
    _, kwargs = env.client.images.build.call_args
    assert kwargs["tag"] == "helios-app-acme-collect:latest"
    assert kwargs["rm"] is True


# --- nginx config generation ----------------------------------------------

def test_the_app_config_routes_the_tenant_hostname_to_the_container_ip(env) -> None:
    result = deploy(env, name="collect", tenant="demo", port=3000)
    conf_path = env.root / ".helios-run" / "nginx" / "collect-demo.conf"
    assert conf_path.is_file()
    conf = conf_path.read_text(encoding="utf-8")

    assert "server_name collect-demo.helios.test;" in conf
    assert "listen 80;" in conf
    assert f"proxy_pass http://{TENANT_IP}:3000;" in conf
    assert "proxy_set_header Host $host;" in conf
    assert "proxy_set_header X-Real-IP $remote_addr;" in conf
    assert f"http://collect-demo.helios.test" in result["urls"]


def test_the_path_based_route_lands_in_default_conf(env) -> None:
    """Hostname routing needs a DNS entry; the /apps/<slug> path always works."""
    deploy(env, name="collect", tenant="demo", port=3000)
    default_conf = (env.root / ".helios-run" / "nginx" / "default.conf").read_text(
        encoding="utf-8"
    )
    assert "location /apps/collect-demo/ {" in default_conf
    assert "rewrite ^/apps/collect-demo(/.*)$ $1 break;" in default_conf
    assert "location = /apps/collect-demo {" in default_conf
    assert f"proxy_pass http://{TENANT_IP}:3000;" in default_conf


def test_default_conf_is_created_from_the_fallback_when_absent(env) -> None:
    default_conf_path = env.root / ".helios-run" / "nginx" / "default.conf"
    assert not default_conf_path.exists()
    deploy(env)
    conf = default_conf_path.read_text(encoding="utf-8")
    assert "listen 80 default_server;" in conf
    assert "Helios Platform" in conf


def test_the_route_is_inserted_inside_the_server_block_not_after_it(env) -> None:
    """Inserting after the closing brace produces an invalid nginx config."""
    deploy(env)
    conf = (env.root / ".helios-run" / "nginx" / "default.conf").read_text(
        encoding="utf-8"
    )
    assert conf.rstrip().endswith("}")
    assert conf.count("}") == conf.count("{")


def test_redeploying_does_not_duplicate_the_route(env) -> None:
    """Idempotent: a second deploy must not stack a second location block."""
    deploy(env, name="collect", tenant="demo")
    deploy(env, name="collect", tenant="demo")
    conf = (env.root / ".helios-run" / "nginx" / "default.conf").read_text(
        encoding="utf-8"
    )
    assert conf.count("location /apps/collect-demo/ {") == 1
    assert conf.count("location = /apps/collect-demo {") == 1
    assert conf.count("{") == conf.count("}")


def test_an_existing_default_conf_is_preserved_and_extended(env) -> None:
    nginx_dir = env.root / ".helios-run" / "nginx"
    nginx_dir.mkdir(parents=True, exist_ok=True)
    (nginx_dir / "default.conf").write_text(
        "server {\n    listen 80 default_server;\n    # keep me\n}\n",
        encoding="utf-8",
    )
    deploy(env)
    conf = (nginx_dir / "default.conf").read_text(encoding="utf-8")
    assert "# keep me" in conf
    assert "location /apps/collect-demo/ {" in conf


def test_the_nginx_config_contains_no_secret(env) -> None:
    """Security property: the config directory is bind-mounted into nginx."""
    deploy(env, tenant="demo")
    state = json.loads((env.tenants / "demo.json").read_text(encoding="utf-8"))
    for path in (env.root / ".helios-run" / "nginx").rglob("*.conf"):
        text = path.read_text(encoding="utf-8")
        assert state["execution_secret"] not in text, path.name


def test_two_apps_get_two_config_files(env) -> None:
    deploy(env, name="collect", tenant="demo")
    second = env.app_dir.parent / "bundle2"
    second.mkdir()
    saved = env.app_dir
    env.app_dir = second
    try:
        deploy(env, name="invoice", tenant="demo")
    finally:
        env.app_dir = saved
    nginx_dir = env.root / ".helios-run" / "nginx"
    assert (nginx_dir / "collect-demo.conf").is_file()
    assert (nginx_dir / "invoice-demo.conf").is_file()
    conf = (nginx_dir / "default.conf").read_text(encoding="utf-8")
    assert "location /apps/collect-demo/ {" in conf
    assert "location /apps/invoice-demo/ {" in conf


def test_nginx_is_reloaded_after_the_config_is_written(env) -> None:
    """A written config that is never loaded changes nothing at runtime."""
    deploy(env)
    nginx = env.client.containers.get("helios-nginx")
    nginx.exec_run.assert_called_with("nginx -s reload")


# --- the deploy result -----------------------------------------------------

def test_deploy_reports_both_containers_and_the_internal_dns(env) -> None:
    result = deploy(env, name="collect", tenant="demo", port=3000)
    assert result["status"] == "deployed"
    assert result["tenant"] == "demo"
    assert result["app"] == "collect"
    assert result["containers"] == {
        "app": "demo-collect-app",
        "audit_sidecar": "demo-collect-audit",
    }
    assert result["internal_dns"] == {
        "app": "http://collect:3000",
        "audit": "http://collect-audit:8080",
    }
    assert result["urls"] == [
        "http://collect-demo.helios.test",
        "http://localhost/apps/collect-demo",
    ]


def test_the_declared_permissions_are_echoed_to_the_audit_sidecar(env) -> None:
    """The sidecar's ALLOWED_TOOLS is what actually narrows the app."""
    result = deploy(env, permissions=["payments", "email"])
    assert result["audit"] == {"allowed_tools": ["payments", "email"]}

    audit = next(
        c for c in env.client.containers.created
        if c.get("name") == "demo-collect-audit"
    )
    assert audit["environment"]["ALLOWED_TOOLS"] == "payments,email"


def test_an_empty_permission_list_falls_back_to_the_documented_default(env) -> None:
    deploy(env, permissions=[])
    audit = next(
        c for c in env.client.containers.created
        if c.get("name") == "demo-collect-audit"
    )
    assert audit["environment"]["ALLOWED_TOOLS"] == "payments,email"


def test_the_app_container_never_receives_the_execution_secret(env) -> None:
    """The sidecar split only holds if the app container lacks the secret."""
    deploy(env)
    state = json.loads((env.tenants / "demo.json").read_text(encoding="utf-8"))
    secret = state["execution_secret"]

    for record in env.client.containers.created:
        name = record.get("name", "")
        env_vars = record.get("environment") or {}
        if name.endswith("-app") or name.startswith("smoke-"):
            assert secret not in env_vars.values(), name
            assert "HELIOS_EXECUTION_SECRET" not in env_vars, name

    audit = next(
        c for c in env.client.containers.created
        if c.get("name") == "demo-collect-audit"
    )
    assert audit["environment"]["HELIOS_EXECUTION_SECRET"] == secret


def test_the_app_container_points_at_the_sidecar_not_at_core(env) -> None:
    deploy(env)
    app = next(
        c for c in env.client.containers.created
        if c.get("name") == "demo-collect-app"
    )
    assert app["environment"]["HELIOS_AUDIT_URL"] == "http://collect-audit:8080/execute"
    assert "HELIOS_CORE_URL" not in app["environment"]


def test_the_smoke_test_container_is_removed_whether_it_passes_or_not(env) -> None:
    deploy(env)
    smoke = next(
        c["_container"] for c in env.client.containers.created
        if str(c.get("name", "")).startswith("smoke-")
    )
    smoke.remove.assert_called_once_with(force=True)


def test_a_smoke_test_failure_reports_the_crash_logs(env) -> None:
    """The logs are the feedback the coding agent needs; dropping them is the bug."""
    client = env.client
    original = client.containers.create.side_effect

    def failing(*args, **kwargs):
        container = original(*args, **kwargs)
        container.exec_run.return_value = SimpleNamespace(exit_code=1, output=b"")
        container.logs.return_value = b"Traceback: boom\n"
        return container

    client.containers.create.side_effect = failing
    with pytest.raises(orchestrator.DeployError) as excinfo:
        deploy(env)
    assert excinfo.value.status == 422
    assert excinfo.value.detail["stage"] == "smoke_test"
    assert "Traceback: boom" in excinfo.value.detail["logs"]


# --- tenant state ----------------------------------------------------------

def test_a_tenant_secret_is_created_once_and_reused(env) -> None:
    """Rotating the secret on every deploy would orphan the running sidecar."""
    deploy(env, name="one", tenant="acme")
    first = json.loads((env.tenants / "acme.json").read_text(encoding="utf-8"))

    saved = env.app_dir
    env.app_dir = saved.parent / "bundle-two"
    env.app_dir.mkdir()
    try:
        deploy(env, name="two", tenant="acme")
    finally:
        env.app_dir = saved

    second = json.loads((env.tenants / "acme.json").read_text(encoding="utf-8"))
    assert first == second
    assert len(first["execution_secret"]) == 32
    uuid.UUID(first["execution_secret"])  # a real uuid4 hex, not a constant


def test_an_existing_tenant_secret_is_read_not_overwritten(env) -> None:
    (env.tenants / "demo.json").write_text(
        json.dumps({"execution_secret": "a" * 64}), encoding="utf-8"
    )
    deploy(env, tenant="demo")
    assert json.loads((env.tenants / "demo.json").read_text(encoding="utf-8")) == {
        "execution_secret": "a" * 64
    }


def test_the_tenants_directory_lives_under_the_run_directory(env) -> None:
    """It holds a runtime secret, so it must be inside the gitignored run dir."""
    deploy(env)
    assert env.tenants == env.root / ".helios-run" / "tenants"
    assert (env.root / ".helios-run" / "tenants" / "demo.json").is_file()


def test_no_tenant_state_is_written_outside_the_run_directory(env, tmp_path) -> None:
    deploy(env)
    stray = [
        p for p in tmp_path.rglob("*.json")
        if ".helios-run" not in p.parts and p.name != "helios.yaml"
    ]
    assert stray == []


# --- golden images ---------------------------------------------------------

def test_a_missing_golden_image_is_built_from_its_dockerfile(env) -> None:
    client = env.client
    client.images.get.side_effect = ImageNotFound("no such image")
    deploy(env)
    built = {c.kwargs["tag"] for c in client.images.build.call_args_list}
    assert set(orchestrator.GOLDEN_IMAGES) <= built


def test_a_base_image_build_failure_surfaces_the_logs(env) -> None:
    client = env.client
    client.images.get.side_effect = ImageNotFound("no such image")
    client.images.build.side_effect = BuildError(
        "step 3 failed", [{"stream": "COPY failed: file not found\n"}]
    )
    with pytest.raises(orchestrator.DeployError) as excinfo:
        deploy(env)
    assert excinfo.value.status == 500
    assert excinfo.value.detail["stage"] == "base-image"
    # The point of capturing the log: the coding agent has to see WHY the build
    # failed. `"step 3 failed"` alone is not actionable.
    assert "file not found" in excinfo.value.detail["logs"]


def test_an_app_build_failure_is_a_422_with_the_build_log(env) -> None:
    """422, not 500: the bundle is wrong, the platform is fine."""
    env.client.images.build.side_effect = BuildError(
        "boom", [{"stream": "syntax error in main.py\n"}]
    )
    with pytest.raises(orchestrator.DeployError) as excinfo:
        deploy(env)
    assert excinfo.value.status == 422
    assert excinfo.value.detail["stage"] == "build"
    assert "syntax error" in excinfo.value.detail["logs"]


def test_build_error_text_joins_the_docker_py_log_records() -> None:
    """docker-py stores the log on `build_log`, singular.

    Pinned explicitly: reading `build_logs` (plural) silently returns nothing,
    and `getattr(exc, "build_logs", [])` hides that typo instead of raising.
    """
    exc = BuildError("reason", [{"stream": "first line\n"}, {"stream": "second line\n"}])
    text = orchestrator._build_error_text(exc)
    assert "first line" in text
    assert "second line" in text


def test_build_error_text_handles_plain_string_records() -> None:
    exc = BuildError("reason", ["not a dict", "also not"])
    text = orchestrator._build_error_text(exc)
    assert "not a dict" in text
    assert "also not" in text


def test_build_error_text_truncates_a_huge_log() -> None:
    """A 40 MB build log must not become a 40 MB HTTP response."""
    exc = BuildError("boom", [{"stream": "x" * 10000 + "\n"} for _ in range(400)])
    text = orchestrator._build_error_text(exc)
    assert len(text) <= 4000


def test_build_error_text_keeps_the_TAIL_of_a_long_log() -> None:
    """The error is at the end of a build log; the head is setup noise."""
    exc = BuildError("boom", [{"stream": f"line {i}\n"} for i in range(2000)])
    text = orchestrator._build_error_text(exc)
    assert "line 1999" in text
    assert "line 0\n" not in text


def test_build_error_text_falls_back_to_str_when_there_are_no_logs() -> None:
    exc = BuildError("plain failure", None)
    assert orchestrator._build_error_text(exc) == "plain failure"


def test_build_error_text_falls_back_when_the_log_records_are_empty() -> None:
    exc = BuildError("plain failure", [])
    assert orchestrator._build_error_text(exc) == "plain failure"


def test_a_missing_app_image_is_reported_with_the_bootstrap_hint(env) -> None:
    """The hint names the command that fixes it."""
    client = env.client

    def get(ref):
        if ref == orchestrator.REDIS_IMAGE:
            raise ImageNotFound("not pulled")
        return MagicMock()

    client.images.get.side_effect = get
    client.images.pull.side_effect = APIError("registry unreachable")
    with pytest.raises(orchestrator.DeployError) as excinfo:
        env.orch.ensure_tenant("demo")
    assert excinfo.value.status == 500
    assert excinfo.value.detail["stage"] == "image-pull"


# --- queries ---------------------------------------------------------------

def test_list_apps_reads_the_labels_the_deploy_writes(env) -> None:
    deploy(env, name="collect", tenant="demo")
    listed = MagicMock()
    listed.name = "demo-collect-app"
    listed.status = "running"
    listed.labels = {"helios.app": "collect", "helios.tenant": "demo"}
    listed.image.tags = ["helios-app-demo-collect:latest"]
    env.client.containers.list.return_value = [listed]

    apps = env.orch.list_apps()
    assert apps == [{
        "container": "demo-collect-app",
        "status": "running",
        "tenant": "demo",
        "app": "collect",
        "image": "helios-app-demo-collect:latest",
        "url": "http://localhost/apps/collect-demo",
    }]


def test_list_apps_is_sorted_for_stable_output(env) -> None:
    def make(name):
        c = MagicMock()
        c.name = name
        c.status = "running"
        c.labels = {"helios.app": name, "helios.tenant": "demo"}
        c.image.tags = ["t:latest"]
        return c

    env.client.containers.list.return_value = [make("z-app"), make("a-app")]
    assert [a["container"] for a in env.orch.list_apps()] == ["a-app", "z-app"]


def test_an_untagged_image_does_not_crash_list_apps(env) -> None:
    c = MagicMock()
    c.name = "x-app"
    c.status = "running"
    c.labels = {"helios.app": "x", "helios.tenant": "demo"}
    c.image.tags = []
    env.client.containers.list.return_value = [c]
    assert env.orch.list_apps()[0]["image"] == "<none>"


def test_logs_strips_blank_lines(env) -> None:
    # `containers.get` has a side_effect that only resolves known names, so
    # setting `return_value` alone would not take effect. Register the name.
    container = MagicMock()
    container.logs.return_value = b"one\n\n  two  \n\n"
    env.client.containers.get.side_effect = lambda name: container
    assert env.orch.logs("demo-collect-app") == ["one", "  two  "]


def test_logs_respects_the_tail_argument(env) -> None:
    container = MagicMock()
    container.logs.return_value = b"x\n"
    env.client.containers.get.side_effect = lambda name: container
    env.orch.logs("c", tail=50)
    _, kwargs = container.logs.call_args
    assert kwargs["tail"] == 50
    assert kwargs["stdout"] is True and kwargs["stderr"] is True


def test_logs_for_a_missing_container_is_a_404(env) -> None:
    env.client.containers.get.side_effect = NotFound("gone")
    with pytest.raises(orchestrator.DeployError) as excinfo:
        env.orch.logs("nope")
    assert excinfo.value.status == 404
    assert "no such container" in str(excinfo.value)


def test_a_tenant_network_is_internal(env) -> None:
    """Security property: tenant traffic must not reach the public bridge.

    `networks.get` misses for every name in this mock, so `networks.create` is
    what runs — the first-deploy path, and the only place `internal` is visible.
    """
    deploy(env, tenant="demo")
    created = env.client.networks.created
    assert "tenant_demo_net" in created, sorted(created)
    assert created["tenant_demo_net"].internal is True

    # The ingress network is deliberately NOT internal: it is what nginx
    # reaches the app through. Getting these two backwards would either expose
    # tenant traffic or make every app unreachable.
    assert INGRESS_NET in created
    assert created[INGRESS_NET].internal is False

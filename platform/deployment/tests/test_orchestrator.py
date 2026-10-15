"""Offline tests for the orchestrator's routing, rollback and base-image logic.

The docker client is mocked throughout. These tests assert the three properties
the imported Mac-mini build violated, each of which is invisible in a single
successful deploy and only shows up on a redeploy or a failure:

  * nginx routing reflects the CURRENT container IP, not the first one seen
  * a failed candidate leaves the previous deployment running
  * the golden base image is rebuilt when platform source changes

No Docker daemon, no network, no image builds.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engine import orchestrator as orch_mod
from engine.contracts import Manifest


# --------------------------------------------------------------------------
# nginx routing
# --------------------------------------------------------------------------
@pytest.fixture
def nginx_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the module's nginx directory at a temp dir."""
    target = tmp_path / "nginx"
    monkeypatch.setattr(orch_mod, "NGINX_DIR", target)
    return target


def _orchestrator_without_docker() -> orch_mod.Orchestrator:
    """An Orchestrator whose client is a mock, skipping the daemon ping."""
    obj = orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)
    obj.client = MagicMock()
    return obj


def test_route_written_for_new_slug(nginx_dir: Path):
    o = _orchestrator_without_docker()
    o.write_nginx_route("myapp-t1", "myapp-t1.helios.test", "172.20.0.9", 3000)

    slug_conf = (nginx_dir / "myapp-t1.conf").read_text()
    default_conf = (nginx_dir / "default.conf").read_text()

    assert "server_name myapp-t1.helios.test" in slug_conf
    assert "set $upstream http://172.20.0.9:3000" in slug_conf
    # Both routing files carry the IP -- the imported build only refreshed one.
    assert "set $upstream http://172.20.0.9:3000" in default_conf
    assert "location /apps/myapp-t1/" in default_conf
    assert "# helios:begin myapp-t1" in default_conf
    assert "# helios:end myapp-t1" in default_conf
    # The prefix must be stripped so the app can serve relative URLs.
    assert "rewrite ^/apps/myapp-t1(/.*)$ $1 break;" in default_conf
    # The seed's default location must survive the insertion.
    assert "listen 80 default_server" in default_conf


def test_redeploy_replaces_stale_ip(nginx_dir: Path):
    """The bug this fixes: a redeploy kept the OLD ingress IP forever.

    The imported build inserted the location block only when
    `location /apps/{slug}` was absent, so the second deploy -- onto a different
    IP -- left nginx pointing at a container that no longer existed.
    """
    o = _orchestrator_without_docker()
    o.write_nginx_route("myapp-t1", "myapp-t1.helios.test", "172.20.0.9", 3000)
    o.write_nginx_route("myapp-t1", "myapp-t1.helios.test", "172.20.0.44", 3000)

    default_conf = (nginx_dir / "default.conf").read_text()
    slug_conf = (nginx_dir / "myapp-t1.conf").read_text()

    assert "172.20.0.44" in default_conf, "default.conf must carry the new IP"
    assert "172.20.0.9" not in default_conf, "the stale IP must be gone"
    assert "172.20.0.44" in slug_conf
    assert "172.20.0.9" not in slug_conf


def test_redeploy_does_not_duplicate_blocks(nginx_dir: Path):
    o = _orchestrator_without_docker()
    for ip in ("172.20.0.2", "172.20.0.3", "172.20.0.4"):
        o.write_nginx_route("myapp-t1", "myapp-t1.helios.test", ip, 3000)
    conf = (nginx_dir / "default.conf").read_text()
    assert conf.count("# helios:begin myapp-t1") == 1
    assert conf.count("location /apps/myapp-t1/") == 1
    assert "172.20.0.4" in conf
    # Exactly one closing brace for the server block: no splicing.
    assert conf.rstrip().endswith("}")


def test_two_slugs_coexist_and_do_not_collide(nginx_dir: Path):
    """Marker delimiting must keep one app's block out of another's."""
    o = _orchestrator_without_docker()
    o.write_nginx_route("appA-t1", "appA-t1.helios.test", "172.20.0.10", 3000)
    o.write_nginx_route("appB-t1", "appB-t1.helios.test", "172.20.0.11", 3001)
    # Redeploy A only; B must be untouched.
    o.write_nginx_route("appA-t1", "appA-t1.helios.test", "172.20.0.99", 3000)

    conf = (nginx_dir / "default.conf").read_text()
    assert "172.20.0.99" in conf and "172.20.0.10" not in conf
    assert "172.20.0.11:3001" in conf, "appB's route must be unchanged"
    assert conf.count("# helios:begin appA-t1") == 1
    assert conf.count("# helios:begin appB-t1") == 1


def test_undeploy_strips_only_its_own_block(nginx_dir: Path):
    o = _orchestrator_without_docker()
    o.write_nginx_route("appA-t1", "appA-t1.helios.test", "172.20.0.10", 3000)
    o.write_nginx_route("appB-t1", "appB-t1.helios.test", "172.20.0.11", 3001)
    o._strip_nginx_block("appA-t1")
    conf = (nginx_dir / "default.conf").read_text()
    assert "appA-t1" not in conf
    assert "172.20.0.11:3001" in conf


def test_corrupt_default_conf_fails_closed(nginx_dir: Path):
    """A config with no closing brace must not be silently mangled."""
    (nginx_dir).mkdir(parents=True, exist_ok=True)
    (nginx_dir / "default.conf").write_text("server { listen 80;\n")
    o = _orchestrator_without_docker()
    with pytest.raises(orch_mod.DeployError) as excinfo:
        o.write_nginx_route("myapp-t1", "h", "1.2.3.4", 3000)
    assert excinfo.value.detail["stage"] == "routing"
    assert "closing brace" in excinfo.value.detail["error"]


def test_reload_validates_config_before_applying(nginx_dir: Path):
    """nginx -t must run first: reloading a broken config drops all routing."""
    o = _orchestrator_without_docker()
    nginx = MagicMock()
    o.client.containers.get.return_value = nginx
    nginx.exec_run.return_value = MagicMock(exit_code=1, output=b"nginx: [emerg] bad;")

    with pytest.raises(orch_mod.DeployError) as excinfo:
        o.reload_nginx()
    detail = excinfo.value.detail
    assert detail["stage"] == "routing"
    assert "nginx -t" in detail["error"]
    assert "not reloaded" in detail["hint"]
    # The reload signal must never be sent after a failed validation.
    assert nginx.exec_run.call_count == 1


def test_reload_tolerates_absent_nginx(nginx_dir: Path):
    from docker.errors import NotFound
    o = _orchestrator_without_docker()
    o.client.containers.get.side_effect = NotFound("no nginx")
    o.reload_nginx()  # must not raise


# --------------------------------------------------------------------------
# ingress IP retry
# --------------------------------------------------------------------------
def test_ip_read_retries_until_assigned():
    """Reading the IP once, immediately after start(), can yield ''."""
    o = _orchestrator_without_docker()
    container = MagicMock()
    container.name = "t1-app"
    states = [
        {"NetworkSettings": {"Networks": {"helios_ingress_net": {"IPAddress": ""}}}},
        {"NetworkSettings": {"Networks": {"helios_ingress_net": {"IPAddress": ""}}}},
        {"NetworkSettings": {"Networks": {"helios_ingress_net": {"IPAddress": "172.20.0.7"}}}},
    ]
    container.attrs = states[0]
    container.reload.side_effect = lambda: setattr(container, "attrs", states.pop(0))
    with patch.object(orch_mod.time, "sleep", return_value=None):
        assert o._network_ip(container, "helios_ingress_net") == "172.20.0.7"


def test_ip_read_fails_loudly_when_never_assigned():
    """An empty IP written into nginx 502s later with no explanation."""
    o = _orchestrator_without_docker()
    container = MagicMock()
    container.name = "t1-app"
    container.attrs = {"NetworkSettings": {"Networks": {"helios_ingress_net": {"IPAddress": ""}}}}
    container.reload.return_value = None
    with patch.object(orch_mod.time, "sleep", return_value=None):
        with pytest.raises(orch_mod.DeployError) as excinfo:
            o._network_ip(container, "helios_ingress_net")
    detail = excinfo.value.detail
    assert detail["stage"] == "network"
    assert "t1-app" in detail["error"]
    assert "docker network inspect" in detail["hint"]


# --------------------------------------------------------------------------
# rollback
# --------------------------------------------------------------------------
def _healthy(container: MagicMock) -> None:
    container.status = "running"
    container.exec_run.return_value = MagicMock(exit_code=0)


def _unhealthy(container: MagicMock) -> None:
    container.status = "running"
    container.exec_run.return_value = MagicMock(exit_code=1)
    container.logs.return_value = b"Traceback: app crashed"


def _drive_deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                  *, app_healthy: bool, audit_healthy: bool = True,
                  manifest: Manifest | None = None):
    """Run Orchestrator.deploy against a fully mocked daemon.

    Returns (orchestrator, created, removed, nginx_routes) where `created` maps
    container name -> mock, `removed` is the list of names that were torn down,
    and `nginx_routes` records the IPs handed to write_nginx_route.

    Everything Docker-facing is stubbed: no daemon, no builds, no network.
    """
    app_dir = tmp_path / "app"
    app_dir.mkdir(exist_ok=True)
    (app_dir / "server.py").write_text("app = None\n")

    o = _orchestrator_without_docker()
    created: dict[str, MagicMock] = {}
    removed: list[str] = []
    nginx_routes: list[tuple] = []

    def fake_container(name, image, networks, env, labels=None, restart=True,
                       extra_hosts=None, volumes=None, replace=True, start=True):
        c = MagicMock()
        c.name = name
        c.attrs = {"NetworkSettings": {"Networks": {
            "helios_ingress_net": {"IPAddress": "172.20.0.77"}}}}
        c.status = "running"
        created[name] = c
        return c

    def fake_health(container, path, port, attempts=30):
        name = container.name
        # The isolated smoke-test container must always pass, otherwise the
        # deploy aborts before reaching the candidate/rollback logic under test.
        if name.startswith("smoke-"):
            return (True, "")
        if "audit" in name:
            return (audit_healthy, "" if audit_healthy else "audit crashed")
        return (app_healthy, "" if app_healthy else "app crashed")

    def fake_network(name, internal=False):
        # NOTE: MagicMock(name="x") sets the mock's repr, NOT a `.name`
        # attribute -- the attribute has to be assigned explicitly, or the
        # orchestrator's `ingress.name` lookup silently returns a child mock.
        n = MagicMock()
        n.name = name
        return n

    monkeypatch.setattr(o, "ensure_base_images",
                        lambda: {"tag": "helios-app-base:h", "platform_hash": "h",
                                 "rebuilt": False})
    monkeypatch.setattr(o, "ensure_tenant",
                        lambda t: (fake_network("tenant_t1_net"),
                                   {"execution_secret": "sec"}))
    monkeypatch.setattr(o, "_network", fake_network)
    monkeypatch.setattr(o, "_container", fake_container)
    monkeypatch.setattr(o, "_container_health_ok", fake_health)
    monkeypatch.setattr(o, "_inject_policy", lambda c, text: None)
    monkeypatch.setattr(o, "write_nginx_route",
                        lambda slug, host, ip, port: nginx_routes.append((slug, ip, port)))
    monkeypatch.setattr(o, "reload_nginx", lambda: None)
    monkeypatch.setattr(o, "_remove_if_exists", lambda name: removed.append(name))
    o.client.images.build.return_value = MagicMock()
    o.client.containers.create.side_effect = (
        lambda image, name, network=None, environment=None: fake_container(
            name, image, {}, environment or {}))
    return o, created, removed, nginx_routes, app_dir, (manifest or Manifest(
        name="myapp", tenant="t1", entry="server:app", port=3000,
        permissions=["payments"]))


def test_unhealthy_candidate_leaves_live_deployment_running(tmp_path, monkeypatch):
    """The rollback guarantee, driven through the real deploy() path.

    The imported build force-removed the running app and auditor BEFORE starting
    the new ones, so a bad redeploy took a working deployment down with it. Now
    the candidates are health-checked first, and on failure only they are
    removed.
    """
    o, created, removed, routes, app_dir, manifest = _drive_deploy(
        tmp_path, monkeypatch, app_healthy=False)

    with pytest.raises(orch_mod.DeployError) as excinfo:
        o.deploy("t1", manifest, app_dir)

    detail = excinfo.value.detail
    assert detail["stage"] == "smoke_test"
    assert "existing deployment left running" in detail["error"]
    assert "app crashed" in detail["logs"]

    # Candidates were created and torn down...
    assert "t1-myapp-app-next" in created
    created["t1-myapp-app-next"].remove.assert_called_once_with(force=True)
    created["t1-myapp-audit-next"].remove.assert_called_once_with(force=True)
    # ...and the live pair was never removed, and routing was never repointed.
    assert removed == [], f"live containers were removed: {removed}"
    assert routes == [], "nginx must not be repointed at a failed candidate"


def test_unhealthy_audit_sidecar_also_aborts(tmp_path, monkeypatch):
    """The sidecar is the platform's last gate; it must be healthy too."""
    o, created, removed, routes, app_dir, manifest = _drive_deploy(
        tmp_path, monkeypatch, app_healthy=True, audit_healthy=False)
    with pytest.raises(orch_mod.DeployError) as excinfo:
        o.deploy("t1", manifest, app_dir)
    assert "audit sidecar candidate never passed /health" in excinfo.value.detail["error"]
    assert removed == [] and routes == []


def test_successful_deploy_promotes_and_routes(tmp_path, monkeypatch):
    o, created, removed, routes, app_dir, manifest = _drive_deploy(
        tmp_path, monkeypatch, app_healthy=True)
    result = o.deploy("t1", manifest, app_dir)

    assert result["status"] == "deployed"
    assert result["tenant"] == "t1" and result["app"] == "myapp"
    assert result["base_image"]["platform_hash"] == "h"
    # Old pair removed, then candidates renamed into their production names.
    assert removed == ["t1-myapp-audit", "t1-myapp-app"]
    created["t1-myapp-audit-next"].rename.assert_called_once_with("t1-myapp-audit")
    created["t1-myapp-app-next"].rename.assert_called_once_with("t1-myapp-app")
    # Routing carries the real IP, not an empty string.
    # routed by container NAME, not IP, so a restart cannot strand the route
    assert routes == [("myapp-t1", "t1-myapp-app", 3000)]
    assert result["urls"][-1] == "http://localhost/apps/myapp-t1/"
    # Secret material is never echoed back; only names are.
    assert result["audit"]["env_forwarded"] == []
    assert "sec" not in json.dumps(result)


def test_container_creation_can_refuse_to_replace():
    """replace=False must not delete a running container of the same name."""
    o = _orchestrator_without_docker()
    from docker.errors import NotFound
    existing = MagicMock()
    o.client.containers.get.return_value = existing
    o.client.networks.get.return_value = MagicMock()
    o.client.containers.create.return_value = MagicMock()

    o._container("t1-app-app", "img", {}, {}, replace=False)
    existing.remove.assert_not_called()

    o._container("t1-app-app", "img", {}, {}, replace=True)
    existing.remove.assert_called_once_with(force=True)


# --------------------------------------------------------------------------
# base image hash
# --------------------------------------------------------------------------
def _write_platform(root: Path, payload: str) -> None:
    pkg = root / "platform" / "audit-agent-builder" / "src" / "audit_agent"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(payload)


def test_platform_hash_changes_with_source(tmp_path: Path):
    _write_platform(tmp_path, "VERSION = 1\n")
    first = orch_mod.platform_hash(tmp_path / "platform")
    _write_platform(tmp_path, "VERSION = 2\n")
    second = orch_mod.platform_hash(tmp_path / "platform")
    assert first != second, "a platform edit must invalidate the base image"


def test_platform_hash_ignores_tests_and_caches(tmp_path: Path):
    """Tests run on every commit; they must not force a rebuild."""
    _write_platform(tmp_path, "VERSION = 1\n")
    base = orch_mod.platform_hash(tmp_path / "platform")

    tests = tmp_path / "platform" / "audit-agent-builder" / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_x.py").write_text("def test_x(): assert True\n")
    cache = tmp_path / "platform" / "audit-agent-builder" / "src" / "audit_agent" / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "x.cpython-313.pyc").write_bytes(b"\x00\x01")

    assert orch_mod.platform_hash(tmp_path / "platform") == base


def test_platform_hash_is_stable_across_calls(tmp_path: Path):
    _write_platform(tmp_path, "VERSION = 1\n")
    a = orch_mod.platform_hash(tmp_path / "platform")
    b = orch_mod.platform_hash(tmp_path / "platform")
    assert a == b and len(a) == 16


def test_platform_hash_detects_added_package(tmp_path: Path):
    _write_platform(tmp_path, "VERSION = 1\n")
    before = orch_mod.platform_hash(tmp_path / "platform")
    other = tmp_path / "platform" / "agent-integration" / "src" / "agent_integration"
    other.mkdir(parents=True, exist_ok=True)
    (other / "__init__.py").write_text("X = 1\n")
    assert orch_mod.platform_hash(tmp_path / "platform") != before


def test_base_image_reused_when_hash_tag_exists():
    o = _orchestrator_without_docker()
    o.client.images.get.return_value = MagicMock(tags=["helios-app-base:abc"])
    with patch.object(orch_mod, "platform_hash", return_value="abc"):
        info = o._ensure_base_image()
    assert info == {"tag": "helios-app-base:abc", "platform_hash": "abc", "rebuilt": False}
    o.client.images.build.assert_not_called()


def test_base_image_rebuilt_when_hash_tag_missing():
    from docker.errors import ImageNotFound
    o = _orchestrator_without_docker()
    o.client.images.get.side_effect = ImageNotFound("nope")
    o.client.images.build.return_value = MagicMock()
    with patch.object(orch_mod, "platform_hash", return_value="def"):
        info = o._ensure_base_image()
    assert info["rebuilt"] is True and info["platform_hash"] == "def"
    kwargs = o.client.images.build.call_args.kwargs
    # Context must be the REPO ROOT: the base image copies platform/* into it.
    assert kwargs["path"] == str(orch_mod.REPO)
    assert kwargs["dockerfile"] == "platform/deployment/images/app-base/Dockerfile"
    assert kwargs["tag"] == "helios-app-base:def"


# --------------------------------------------------------------------------
# repo root discovery
# --------------------------------------------------------------------------
def test_repo_root_has_platform_and_specs():
    assert (orch_mod.REPO / "platform").is_dir()
    assert (orch_mod.REPO / "specs").is_dir()


def test_repo_root_env_override_rejects_missing_dir(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HELIOS_REPO_ROOT", "/nonexistent/helios/root")
    with pytest.raises(RuntimeError, match="HELIOS_REPO_ROOT"):
        orch_mod._discover_repo_root()


def test_repo_root_env_override_accepts_valid_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "platform").mkdir()
    (tmp_path / "specs").mkdir()
    monkeypatch.setenv("HELIOS_REPO_ROOT", str(tmp_path))
    assert orch_mod._discover_repo_root() == tmp_path.resolve()


# --------------------------------------------------------------------------
# data volume naming
# --------------------------------------------------------------------------
def test_data_volume_name_is_tenant_and_app_scoped():
    o = _orchestrator_without_docker()
    assert o._data_volume("t1", "myapp") == "helios-data-t1-myapp"
    # Two tenants of the same app must not share a volume: one tenant's audit
    # log must not be readable or writable by the other.
    assert o._data_volume("t2", "myapp") != o._data_volume("t1", "myapp")

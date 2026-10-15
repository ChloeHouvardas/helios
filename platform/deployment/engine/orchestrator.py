"""OrchestratorDriver: all Docker lifecycle control via the Docker Engine API (docker-py).

Maps 1:1 to the deployment pipeline in the Helios design doc:
  1. ingest & static audit (done in app.py before this module)
  2. fast image build on the golden base image (Dockerfile injected server-side)
  3. sandbox smoke test with crash-log capture -> feedback to the coding agent
  4. ecosystem attachment: per-app audit sidecar + tenant network + Redis + core
  5. live routing through Traefik labels (zero reload)
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import docker
from docker.errors import APIError, BuildError, ImageNotFound, NotFound
from docker.models.containers import Container

from .contracts import Manifest, sanitize

REPO = Path(__file__).resolve().parent.parent
RUN = REPO / ".helios-run"
TENANTS_DIR = RUN / "tenants"
TENANTS_DIR.mkdir(parents=True, exist_ok=True)

INGRESS_NET = "helios_ingress_net"
SANDBOX_NET = "helios_sandbox_net"
BASE_IMAGE = "helios-app-base:latest"
AUDIT_IMAGE = "helios-auditor:latest"
CORE_IMAGE = "helios-core:latest"
REDIS_IMAGE = "redis:7-alpine"

GOLDEN_IMAGES = {
    BASE_IMAGE: "images/app-base/Dockerfile",
    AUDIT_IMAGE: "images/auditor/Dockerfile",
    CORE_IMAGE: "images/core/Dockerfile",
}


class DeployError(Exception):
    def __init__(self, status: int, detail):
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


def _build_error_text(exc: BuildError) -> str:
    """The build log, joined and tail-truncated, or the exception message.

    docker-py sets `BuildError.build_log` (singular). This used to read
    `build_logs`, and because the lookup was a `getattr` with a `[]` default the
    typo was silent: the function always fell through to `str(exc)`, so a
    422 build failure reported only `"boom"` and never the compiler output.
    That defeated step 3 of the pipeline, whose whole purpose is feeding crash
    logs back to the coding agent.

    The tail is kept, not the head: in a build log the head is base-image setup
    noise and the actual error is the last thing printed.
    """
    chunks = []
    for part in getattr(exc, "build_log", None) or []:
        chunks.append(part.get("stream", "") if isinstance(part, dict) else str(part))
    return ("\n".join(chunks) or str(exc))[-4000:]


class Orchestrator:
    def __init__(self):
        try:
            self.client = docker.from_env()
            self.client.ping()
        except Exception as exc:  # daemon unreachable / DOCKER_HOST missing
            raise RuntimeError(
                f"Docker daemon unreachable ({exc}). Start it with `colima start` "
                "and export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock"
            ) from exc

    # ------------------------------------------------------------------ utils
    def _network(self, name: str, internal: bool = False):
        try:
            return self.client.networks.get(name)
        except (NotFound, APIError):
            try:
                return self.client.networks.create(name, driver="bridge", internal=internal)
            except APIError as exc:
                if "already exists" in str(exc):
                    return self.client.networks.get(name)
                raise DeployError(500, {"stage": "network", "error": str(exc)}) from exc

    def _image(self, ref: str):
        try:
            return self.client.images.get(ref)
        except ImageNotFound:
            if ref.endswith(":latest") and "/" not in ref and not ref.startswith("helios-"):
                pass
            try:
                if "/" not in ref.split(":")[0]:  # public hub image
                    return self.client.images.pull(ref)
            except APIError as exc:
                raise DeployError(500, {"stage": "image-pull", "image": ref, "error": str(exc)}) from exc
            raise DeployError(500, {"stage": "image-missing", "image": ref,
                                    "hint": "run bootstrap.sh to build Helios images"})

    def _container(self, name: str, image: str, networks: dict, env: dict,
                   labels: dict | None = None, restart: bool = True,
                   extra_hosts: dict | None = None) -> Container:
        # Docker SDK doesn't accept 'networks' in create(), we need to attach networks after creation
        network_list = list(networks.keys()) if networks else []
        first_network = network_list[0] if network_list else None

        kwargs = dict(
            image=image, name=name, environment=env, labels=labels or {},
        )
        if extra_hosts:
            kwargs["extra_hosts"] = extra_hosts
        if first_network:
            kwargs["network"] = first_network
            # Pass aliases for the first network via networking_config
            first_aliases = networks[first_network].get("aliases", []) if isinstance(networks[first_network], dict) else []
            if first_aliases:
                from docker.types import EndpointConfig
                kwargs["networking_config"] = {
                    first_network: EndpointConfig(version=self.client.api._version,
                                                  aliases=first_aliases)
                }
        if restart:
            kwargs["restart_policy"] = {"Name": "unless-stopped"}

        try:
            existing = self.client.containers.get(name)
            existing.remove(force=True)
        except NotFound:
            pass

        c = self.client.containers.create(**kwargs)

        # Connect to additional networks with aliases
        for network_name in network_list[1:]:
            network = self.client.networks.get(network_name)
            net_opts = networks.get(network_name, {})
            aliases = net_opts.get("aliases", []) if isinstance(net_opts, dict) else []
            network.connect(c, aliases=aliases)

        c.start()
        return c

    def _tenant_state(self, tenant: str) -> dict:
        path = TENANTS_DIR / f"{tenant}.json"
        if path.is_file():
            return json.loads(path.read_text())
        state = {"execution_secret": uuid.uuid4().hex}
        path.write_text(json.dumps(state, indent=2))
        return state

    # ------------------------------------------------------- golden images
    def ensure_base_images(self):
        for tag, dockerfile in GOLDEN_IMAGES.items():
            try:
                self.client.images.get(tag)
            except ImageNotFound:
                try:
                    self.client.images.build(path=str(REPO), dockerfile=dockerfile,
                                             tag=tag, rm=True, quiet=True)
                except BuildError as exc:
                    raise DeployError(500, {"stage": "base-image", "image": tag,
                                            "logs": _build_error_text(exc)}) from exc

    # --------------------------------------------------------- tenant infra
    def ensure_tenant(self, tenant: str):
        net = self._network(f"tenant_{tenant}_net", internal=True)
        state = self._tenant_state(tenant)
        self._image(REDIS_IMAGE)
        if not self._is_running(f"helios-redis-{tenant}"):
            self._container(
                f"helios-redis-{tenant}", REDIS_IMAGE,
                {net.name: {"aliases": ["helios-redis"]}}, {},
                labels={"helios.managed": "true", "helios.role": "redis", "helios.tenant": tenant},
            )
        if not self._is_running(f"helios-core-{tenant}"):
            self._container(
                f"helios-core-{tenant}", CORE_IMAGE,
                {net.name: {"aliases": ["helios-core"]}},
                {"HELIOS_EXECUTION_SECRET": state["execution_secret"]},
                labels={"helios.managed": "true", "helios.role": "core", "helios.tenant": tenant},
            )
        return net, state

    def _is_running(self, name: str) -> bool:
        try:
            return self.client.containers.get(name).status == "running"
        except NotFound:
            return False

    # ------------------------------------------------------------ pipeline
    def deploy(self, tenant: str, manifest: Manifest, app_dir: Path) -> dict:
        self.ensure_base_images()
        t = sanitize(tenant or manifest.tenant or "demo")
        a = manifest.name
        port = manifest.port
        net, state = self.ensure_tenant(t)
        ingress = self._network(INGRESS_NET, internal=False)

        image_tag = f"helios-app-{t}-{a}:latest"

        # --- 2. server-side Dockerfile injection + fast build
        dockerfile = (
            f"FROM {BASE_IMAGE}\n"
            "COPY . /app\n"
            "WORKDIR /app\n"
            f'CMD ["uvicorn", "{manifest.entry}", "--host", "0.0.0.0", "--port", "{port}"]\n'
        )
        (app_dir / "Dockerfile").write_text(dockerfile)
        try:
            self.client.images.build(path=str(app_dir), tag=image_tag, rm=True)
        except BuildError as exc:
            raise DeployError(422, {"stage": "build", "error": "image build failed",
                                    "logs": _build_error_text(exc)}) from exc

        # --- 3. sandbox smoke test
        action_token = uuid.uuid4().hex
        sandbox = self._network(SANDBOX_NET, internal=False)
        smoke_name = f"smoke-{t}-{a}-{uuid.uuid4().hex[:6]}"
        smoke_env = {
            "HELIOS_APP_NAME": a, "HELIOS_TENANT": t,
            "HELIOS_ACTION_TOKEN": action_token,
            "HELIOS_AUDIT_URL": "http://helios-audit-smoke:8080/execute",
            "HELIOS_REDIS_URL": "redis://helios-redis:6379/0",
        }
        smoke = self.client.containers.create(
            image_tag, name=smoke_name, network=sandbox.name, environment=smoke_env)
        smoke.start()
        healthy = False
        for _ in range(45):
            smoke.reload()
            if smoke.status in ("exited", "dead"):
                break
            try:
                result = smoke.exec_run(["python", "-c", (
                    "import urllib.request,sys;"
                    f"sys.exit(0 if urllib.request.urlopen("
                    f"'http://127.0.0.1:{port}/health',timeout=2).status==200 else 1)"
                )])
                if result.exit_code == 0:
                    healthy = True
                    break
            except APIError:
                pass
            time.sleep(1)
        if not healthy:
            logs = smoke.logs(stdout=True, stderr=True, tail=200).decode(errors="replace")
            smoke.remove(force=True)
            raise DeployError(422, {"stage": "smoke_test",
                                    "error": "container never passed /health (crash logs below)",
                                    "logs": logs[-4000:]})
        smoke.remove(force=True)

        # --- 4. ecosystem attachment: audit sidecar + app on tenant network
        audit_name = f"{t}-{a}-audit"
        app_name = f"{t}-{a}-app"
        ingress = self._network(INGRESS_NET, internal=False)
        self._container(
            audit_name, AUDIT_IMAGE,
            {net.name: {"aliases": [f"{a}-audit"]}, ingress.name: {}},
            {
                "HELIOS_APP_NAME": a, "HELIOS_TENANT": t,
                "EXPECTED_ACTION_TOKEN": action_token,
                "HELIOS_EXECUTION_SECRET": state["execution_secret"],
                "HELIOS_CORE_URL": "http://helios-core:9000",
                "HELIOS_ENGINE_URL": "http://host.docker.internal:8100",
                "ALLOWED_TOOLS": ",".join(manifest.permissions) or "payments,email",
            },
            labels={"helios.managed": "true", "helios.role": "audit",
                    "helios.tenant": t, "helios.app": a},
            extra_hosts={"host.docker.internal": "host-gateway"},
        )

        # --- 5. live routing via nginx config
        slug = f"{a}-{t}"
        host = f"{slug}.helios.test"
        labels = {
            "helios.managed": "true", "helios.role": "app", "helios.tenant": t, "helios.app": a,
        }
        app_env = {
            "HELIOS_APP_NAME": a, "HELIOS_TENANT": t,
            "HELIOS_ACTION_TOKEN": action_token,
            "HELIOS_AUDIT_URL": f"http://{a}-audit:8080/execute",
            "HELIOS_REDIS_URL": "redis://helios-redis:6379/0",
        }
        app_container = self._container(
            app_name, image_tag,
            {net.name: {"aliases": [a]}, ingress.name: {}},
            app_env, labels=labels,
        )

        # Get app container IP on ingress network
        app_container.reload()
        app_ip = app_container.attrs["NetworkSettings"]["Networks"][ingress.name]["IPAddress"]

        # Generate nginx config with both hostname and path-based routing
        nginx_conf = f"""server {{
    listen 80;
    server_name {host};

    location / {{
        proxy_pass http://{app_ip}:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }}
}}
"""
        nginx_conf_dir = REPO / ".helios-run" / "nginx"
        nginx_conf_dir.mkdir(parents=True, exist_ok=True)
        (nginx_conf_dir / f"{slug}.conf").write_text(nginx_conf)

        # Update default.conf to include path-based routing for this app
        default_conf_path = nginx_conf_dir / "default.conf"
        default_conf = default_conf_path.read_text() if default_conf_path.exists() else """server {
    listen 80 default_server;
    server_name _;

    location / {
        return 200 'Helios Platform - No app deployed at this path';
        add_header Content-Type text/plain;
    }
}
"""
        # Add path-based route to default.conf
        path_route = f"""
    location /apps/{slug}/ {{
        rewrite ^/apps/{slug}(/.*)$ $1 break;
        proxy_pass http://{app_ip}:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }}

    location = /apps/{slug} {{
        proxy_pass http://{app_ip}:{port}/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }}
"""
        # Insert before the final closing brace
        if f"location /apps/{slug}" not in default_conf:
            default_conf = default_conf.rsplit("}", 1)[0] + path_route + "}\n"
            default_conf_path.write_text(default_conf)

        # Reload nginx
        nginx_container = self.client.containers.get("helios-nginx")
        nginx_container.exec_run("nginx -s reload")

        return {
            "status": "deployed",
            "tenant": t,
            "app": a,
            "image": image_tag,
            "containers": {"app": app_name, "audit_sidecar": audit_name},
            "internal_dns": {"app": f"http://{a}:{port}", "audit": f"http://{a}-audit:8080"},
            "urls": [f"http://{host}", f"http://localhost/apps/{slug}"],
            "audit": {"allowed_tools": manifest.permissions},
        }

    # ------------------------------------------------------------- queries
    def list_apps(self) -> list[dict]:
        out = []
        for c in self.client.containers.list(all=True, filters={"label": "helios.role=app"}):
            slug = f"{c.labels.get('helios.app')}-{c.labels.get('helios.tenant')}"
            out.append({
                "container": c.name, "status": c.status,
                "tenant": c.labels.get("helios.tenant"), "app": c.labels.get("helios.app"),
                "image": (c.image.tags or ["<none>"])[0],
                "url": f"http://localhost/apps/{slug}",
            })
        return sorted(out, key=lambda x: x["container"])

    def logs(self, container_name: str, tail: int = 200) -> list[str]:
        try:
            raw = self.client.containers.get(container_name).logs(
                stdout=True, stderr=True, tail=tail).decode(errors="replace")
            return [line for line in raw.splitlines() if line.strip()]
        except NotFound:
            raise DeployError(404, f"no such container: {container_name}")

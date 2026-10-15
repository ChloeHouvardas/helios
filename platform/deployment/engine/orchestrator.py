"""OrchestratorDriver: all Docker lifecycle control via the Docker Engine API (docker-py).

Maps to the deployment pipeline:
  1. ingest & static audit (done in app.py before this module)
  2. fast image build on the golden base image (Dockerfile injected server-side)
  3. sandbox smoke test with crash-log capture -> feedback to the coding agent
  4. ecosystem attachment: per-app audit sidecar + tenant network + Redis + core
  5. live routing through nginx, rewritten on every deploy

Three properties this module is written to keep, all of which the imported
Mac-mini build violated:

**Routing always reflects the running container.** The nginx location block for
a slug is rewritten on every deploy, marker-delimited, rather than appended once
and left stale. A redeploy that lands on a new IP must be reachable at the same
URL.

**A failed redeploy leaves the previous deployment serving.** New containers are
created under temporary names and health-checked *before* anything running is
removed. Only after the new app answers /health on the tenant network is the
swap performed.

**The golden base image tracks the platform source.** `helios-app-base` embeds
the four platform packages, so its tag is a content hash of `platform/`. When
the platform changes, the base image is rebuilt; when it does not, the cached
image is reused. This replaces the imported build's never-rebuild rule, under
which a platform fix silently never reached a deployed app.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

import docker
from docker.errors import APIError, BuildError, ImageNotFound, NotFound
from docker.models.containers import Container

from .contracts import Manifest, resolve_env, sanitize

# --------------------------------------------------------------------------
# Repository root
# --------------------------------------------------------------------------
# The build context for the golden base image must be the REPO ROOT, because the
# base image embeds platform/{action-agent-builder,audit-agent-builder,
# agent-integration,financial-tools} -- none of which live under
# platform/deployment/. The imported build used the engine's own parent as both
# context and runtime dir, which cannot see them.
#
# Resolution order: explicit HELIOS_REPO_ROOT, then walk up looking for a
# directory that contains both platform/ and specs/.
ENGINE_DIR = Path(__file__).resolve().parent          # .../platform/deployment/engine
DEPLOYMENT_DIR = ENGINE_DIR.parent                    # .../platform/deployment


def _discover_repo_root() -> Path:
    env = os.environ.get("HELIOS_REPO_ROOT")
    if env:
        candidate = Path(env).resolve()
        if candidate.is_dir():
            return candidate
        raise RuntimeError(
            f"HELIOS_REPO_ROOT={env} does not exist. Unset it, or point it at "
            f"the repository root (the directory containing platform/ and specs/)."
        )
    for parent in [DEPLOYMENT_DIR, *DEPLOYMENT_DIR.parents]:
        if (parent / "platform").is_dir() and (parent / "specs").is_dir():
            return parent
    # Last resort: the deployment dir's grandparent, i.e. the repo it sits in.
    return DEPLOYMENT_DIR.parent.parent


REPO = _discover_repo_root()
RUN = REPO / ".helios-run"
TENANTS_DIR = RUN / "tenants"
NGINX_DIR = RUN / "nginx"

INGRESS_NET = "helios_ingress_net"
SANDBOX_NET = "helios_sandbox_net"
BASE_IMAGE = "helios-app-base:latest"
AUDIT_IMAGE = "helios-auditor:latest"
CORE_IMAGE = "helios-core:latest"
#: Where the tenant core keeps its SQLite snapshot and market cache.
CORE_DATA_DIR = "/data"
REDIS_IMAGE = "redis:7-alpine"

#: Dockerfile paths are relative to the build context, which is REPO.
GOLDEN_IMAGES = {
    AUDIT_IMAGE: "platform/deployment/images/auditor/Dockerfile",
    CORE_IMAGE: "platform/deployment/images/core/Dockerfile",
}

#: Directories inside platform/ that must not influence the base image hash:
#: they change on every test run and would force a needless rebuild.
_HASH_EXCLUDE_PARTS = {"__pycache__", ".pytest_cache", "tests", ".venv", ".git", "node_modules"}
_HASH_EXCLUDE_SUFFIX = {".pyc", ".pyo", ".egg-info"}

#: How long to wait for a container to be assigned an IP on a network.
_IP_RETRY_ATTEMPTS = 10
_IP_RETRY_DELAY = 0.5

#: nginx location blocks are delimited so a redeploy can replace exactly its own
#: region instead of appending a duplicate or leaving a stale one.
_MARKER_BEGIN = "# helios:begin {slug}"
_MARKER_END = "# helios:end {slug}"

DEFAULT_CONF_SEED = """server {
    listen 80 default_server;
    server_name _;

    location / {
        return 200 'Helios Platform - No app deployed at this path';
        add_header Content-Type text/plain;
    }
}
"""


class DeployError(Exception):
    def __init__(self, status: int, detail):
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


def _build_error_text(exc: BuildError) -> str:
    chunks = []
    # docker-py sets BuildError.build_log (singular); reading build_logs with a
    # [] default silently dropped every compiler line from the deploy response.
    for part in getattr(exc, "build_log", None) or []:
        chunks.append(part.get("stream", "") if isinstance(part, dict) else str(part))
    return ("\n".join(chunks) or str(exc))[-4000:]


# --------------------------------------------------------------------------
# Platform content hash
# --------------------------------------------------------------------------
def _jsonl_files_from_tar(stream, tail: int) -> list[dict]:
    """Collect every .jsonl file in a docker archive stream, verifying chains.

    A file whose entries carry `hash` and `prev_hash` is an audit_agent chain
    and is verified with AuditLog.verify_chain; anything else is an app's
    action log and is returned as-is. Only the last `tail` entries of each
    file are returned, but the chain is verified over the whole file.
    """
    import io
    import tarfile
    import tempfile

    buffer = io.BytesIO()
    for chunk in stream:
        buffer.write(chunk)
    buffer.seek(0)
    files: list[dict] = []
    try:
        archive = tarfile.open(fileobj=buffer, mode="r|*")
    except tarfile.TarError as exc:
        return [{"path": "", "error": f"unreadable archive: {exc}"}]
    with archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith(".jsonl"):
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            raw = handle.read()
            lines = [ln for ln in raw.decode("utf-8", "replace").splitlines() if ln.strip()]
            entries: list = []
            for ln in lines:
                try:
                    entries.append(json.loads(ln))
                except json.JSONDecodeError:
                    entries.append({"unparseable": ln[:200]})
            record: dict = {"path": member.name, "entries": len(entries),
                            "tail": entries[-tail:]}
            if entries and all(isinstance(e, dict) and "hash" in e and "prev_hash" in e
                               for e in entries):
                try:
                    from audit_agent.audit_log import AuditLog
                    with tempfile.NamedTemporaryFile("wb", suffix=".jsonl", delete=False) as tmp:
                        tmp.write(raw)
                        tmp_path = Path(tmp.name)
                    try:
                        ok, problems = AuditLog(tmp_path).verify_chain()
                    finally:
                        tmp_path.unlink(missing_ok=True)
                    record["chain"] = {"verified": True, "intact": ok, "problems": problems[:10]}
                except Exception as exc:  # noqa: BLE001 - report, never hide
                    record["chain"] = {"verified": False, "error": str(exc)[:200]}
            files.append(record)
    return files


def platform_hash(platform_dir: Path | None = None) -> str:
    """Stable hash of the platform source that the golden base image embeds.

    Covers the four packages the base image installs editable. Path + size +
    content are all folded in, sorted by relative path, so the hash does not
    depend on filesystem enumeration order.

    Tests and caches are excluded: they change on every run and would otherwise
    force a rebuild that cannot change runtime behaviour.
    """
    root = platform_dir if platform_dir is not None else REPO / "platform"
    packages = ("action-agent-builder", "audit-agent-builder", "agent-integration", "financial-tools")
    digest = hashlib.sha256()

    for pkg in packages:
        pkg_dir = root / pkg
        if not pkg_dir.is_dir():
            # Record absence too: adding a package later must change the hash.
            digest.update(f"missing:{pkg}\n".encode())
            continue
        files: list[Path] = []
        for path in pkg_dir.rglob("*"):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(root).parts
            if any(part in _HASH_EXCLUDE_PARTS for part in rel_parts):
                continue
            if path.suffix in _HASH_EXCLUDE_SUFFIX:
                continue
            if any(part.endswith(".egg-info") for part in rel_parts):
                continue
            files.append(path)
        for path in sorted(files, key=lambda p: str(p.relative_to(root))):
            rel = str(path.relative_to(root))
            try:
                data = path.read_bytes()
            except OSError:
                digest.update(f"unreadable:{rel}\n".encode())
                continue
            digest.update(f"file:{rel}:{len(data)}\n".encode())
            digest.update(data)
    return digest.hexdigest()[:16]


class Orchestrator:
    def __init__(self):
        try:
            self.client = docker.from_env()
            self.client.ping()
        except Exception as exc:  # daemon unreachable / DOCKER_HOST missing
            raise RuntimeError(
                f"Docker daemon unreachable ({exc}). Start Docker Desktop, or run "
                "`colima start` and export "
                "DOCKER_HOST=unix://$HOME/.colima/default/docker.sock"
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
            try:
                if "/" not in ref.split(":")[0]:  # public hub image
                    return self.client.images.pull(ref)
            except APIError as exc:
                raise DeployError(500, {"stage": "image-pull", "image": ref, "error": str(exc)}) from exc
            raise DeployError(500, {"stage": "image-missing", "image": ref,
                                    "hint": "run bootstrap.sh to build Helios images"})

    def _container(self, name: str, image: str, networks: dict, env: dict,
                   labels: dict | None = None, restart: bool = True,
                   extra_hosts: dict | None = None,
                   volumes: dict | None = None,
                   replace: bool = True,
                   start: bool = True) -> Container:
        """Create, network and (by default) start a container.

        `replace=False` refuses to touch an existing container of the same name.
        The rollback path depends on that: it must never delete the live
        deployment while bringing up a candidate.

        `start=False` leaves the container created but stopped, so a caller can
        `put_archive` into it first. The audit sidecar needs this: it reads its
        policy manifest during startup, and injecting that file after the
        process is already up would leave the sidecar running with no policy.
        """
        from docker.types import EndpointConfig

        network_list = list(networks.keys()) if networks else []
        first_network = network_list[0] if network_list else None

        kwargs: dict = dict(image=image, name=name, environment=env, labels=labels or {})
        if extra_hosts:
            kwargs["extra_hosts"] = extra_hosts
        if volumes:
            kwargs["volumes"] = volumes
        if first_network:
            kwargs["network"] = first_network
            first_aliases = (
                networks[first_network].get("aliases", [])
                if isinstance(networks[first_network], dict) else []
            )
            if first_aliases:
                kwargs["networking_config"] = {
                    first_network: EndpointConfig(version=self.client.api._version,
                                                  aliases=first_aliases)
                }
        if restart:
            kwargs["restart_policy"] = {"Name": "unless-stopped"}

        if replace:
            try:
                existing = self.client.containers.get(name)
                existing.remove(force=True)
            except NotFound:
                pass

        c = self.client.containers.create(**kwargs)

        for network_name in network_list[1:]:
            network = self.client.networks.get(network_name)
            net_opts = networks.get(network_name, {})
            aliases = net_opts.get("aliases", []) if isinstance(net_opts, dict) else []
            network.connect(c, aliases=aliases)

        if start:
            c.start()
        return c

    def _tenant_state(self, tenant: str) -> dict:
        TENANTS_DIR.mkdir(parents=True, exist_ok=True)
        path = TENANTS_DIR / f"{tenant}.json"
        if path.is_file():
            return json.loads(path.read_text())
        state = {"execution_secret": uuid.uuid4().hex,
                 # Signs the core service's intent auth codes. Separate from the
                 # execution secret on purpose: authenticating a caller and
                 # authorising a payment are two different powers.
                 "intent_secret": uuid.uuid4().hex}
        path.write_text(json.dumps(state, indent=2))
        return state

    def _is_running(self, name: str) -> bool:
        try:
            return self.client.containers.get(name).status == "running"
        except NotFound:
            return False

    def _network_ip(self, container: Container, network_name: str) -> str:
        """Read a container's IP on a network, retrying until it is assigned.

        The imported build read this once immediately after start() and wrote
        whatever came back -- including the empty string, producing
        `proxy_pass http://:3000` and a 502 with no explanation. Docker assigns
        the address asynchronously, so retry instead of failing.

        Raises DeployError rather than returning "", because an empty IP written
        into nginx fails silently at request time, long after the deploy reported
        success.
        """
        last = ""
        for attempt in range(_IP_RETRY_ATTEMPTS):
            try:
                container.reload()
                nets = container.attrs.get("NetworkSettings", {}).get("Networks", {}) or {}
                last = (nets.get(network_name) or {}).get("IPAddress", "") or ""
            except (APIError, NotFound):
                last = ""
            if last:
                return last
            time.sleep(_IP_RETRY_DELAY)
        raise DeployError(500, {
            "stage": "network",
            "error": (
                f"container '{container.name}' was not assigned an IP on network "
                f"'{network_name}' after {_IP_RETRY_ATTEMPTS * _IP_RETRY_DELAY:.1f}s"
            ),
            "hint": (
                "The network may be full or the container may be restarting. "
                "Inspect with: docker network inspect " + network_name
            ),
        })

    # ------------------------------------------------------- golden images
    def _ensure_base_image(self) -> dict:
        """Build (or reuse) `helios-app-base` keyed by the platform content hash.

        Tags the result both `helios-app-base:<hash>` and `:latest`. A present
        hash tag is reused without rebuilding, so repeated deploys are fast and
        the layer cache keeps a platform-only change under a minute after the
        first build.
        """
        digest = platform_hash()
        hashed_tag = f"helios-app-base:{digest}"
        dockerfile = "platform/deployment/images/app-base/Dockerfile"

        try:
            self.client.images.get(hashed_tag)
            reused = True
        except ImageNotFound:
            reused = False
            try:
                self.client.images.build(
                    path=str(REPO), dockerfile=dockerfile, tag=hashed_tag, rm=True, quiet=True,
                )
            except BuildError as exc:
                raise DeployError(500, {"stage": "base-image", "image": hashed_tag,
                                        "logs": _build_error_text(exc)}) from exc
        # Repoint :latest at the hash tag so per-app builds (which FROM
        # helios-app-base:latest) always get the current platform.
        try:
            self.client.images.get(hashed_tag).tag("helios-app-base", "latest", force=True)
        except (ImageNotFound, APIError):
            pass
        return {"tag": hashed_tag, "platform_hash": digest, "rebuilt": not reused}

    def ensure_base_images(self) -> dict:
        """Build or reuse every golden image, each keyed by the platform hash.

        The auditor embeds audit-agent-builder and the core embeds
        financial-tools, so a platform change must reach them exactly as it
        reaches app-base: a present hash tag is reused, an absent one is built,
        and `:latest` is repointed so the containers the engine starts always
        run the current platform.
        """
        base = self._ensure_base_image()
        digest = base["platform_hash"]
        for tag, dockerfile in GOLDEN_IMAGES.items():
            repo, _, _ = tag.partition(":")
            hashed_tag = f"{repo}:{digest}"
            try:
                self.client.images.get(hashed_tag)
            except ImageNotFound:
                try:
                    self.client.images.build(path=str(REPO), dockerfile=dockerfile,
                                             tag=hashed_tag, rm=True, quiet=True)
                except BuildError as exc:
                    raise DeployError(500, {"stage": "base-image", "image": tag,
                                            "logs": _build_error_text(exc)}) from exc
            try:
                self.client.images.get(hashed_tag).tag(repo, "latest", force=True)
            except (ImageNotFound, APIError):
                pass
        return base

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
            core_env = {
                "HELIOS_EXECUTION_SECRET": state["execution_secret"],
                "HELIOS_INTENT_SECRET": state.get("intent_secret", ""),
                "HELIOS_TENANT": tenant,
                "HELIOS_DATA_DIR": CORE_DATA_DIR,
                # Live market data only when the engine's own environment holds
                # a token; otherwise the committed fixtures answer and say so.
                "HELIOS_MARKET_MODE": os.environ.get("HELIOS_MARKET_MODE")
                                      or ("auto" if os.environ.get("TUSHARE_TOKEN") else "fixture"),
            }
            if os.environ.get("TUSHARE_TOKEN"):
                core_env["TUSHARE_TOKEN"] = os.environ["TUSHARE_TOKEN"]
            self._container(
                f"helios-core-{tenant}", CORE_IMAGE,
                {net.name: {"aliases": ["helios-core"]}},
                core_env,
                labels={"helios.managed": "true", "helios.role": "core",
                        "helios.tenant": tenant, "helios.data_dir": CORE_DATA_DIR},
                volumes={self._core_volume(tenant): {"bind": CORE_DATA_DIR, "mode": "rw"}},
            )
        return net, state

    # ------------------------------------------------------------ nginx
    def _location_block(self, slug: str, app_ip: str, port: int) -> str:
        """The marker-delimited nginx block for one app slug.

        `app_ip` is the upstream address: the app CONTAINER NAME, which Docker's
        embedded DNS (127.0.0.11) resolves on the ingress network. The upstream
        goes through a variable so nginx resolves it per request rather than once
        at reload; a restarted container gets a new IP, and a literal address would
        answer 502 until the next deploy. `valid=5s` bounds how long a stale answer
        can live.
        """
        begin = _MARKER_BEGIN.format(slug=slug)
        end = _MARKER_END.format(slug=slug)
        return f"""
    {begin}
    location /apps/{slug}/ {{
        rewrite ^/apps/{slug}(/.*)$ $1 break;
        resolver 127.0.0.11 valid=5s ipv6=off;
        set $upstream http://{app_ip}:{port};
        proxy_pass $upstream;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Prefix /apps/{slug};
    }}

    location = /apps/{slug} {{
        resolver 127.0.0.11 valid=5s ipv6=off;
        set $upstream http://{app_ip}:{port}/;
        proxy_pass $upstream;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Prefix /apps/{slug};
    }}
    {end}
"""

    def write_nginx_route(self, slug: str, host: str, app_ip: str, port: int) -> None:
        """Write routing for one app, replacing any previous block for that slug.

        Two files carry the app IP and BOTH must be refreshed on every deploy:
        the per-slug `{slug}.conf` (host-based routing) and the shared
        `default.conf` (path-based routing). The imported build regenerated the
        first and appended-once the second, so a redeploy onto a new IP kept
        serving the dead address at `http://localhost/apps/<slug>/`.

        The block is delimited by markers so replacing it is exact: no duplicate
        blocks accumulate, and one app's route can never be spliced into
        another's.
        """
        NGINX_DIR.mkdir(parents=True, exist_ok=True)
        begin = _MARKER_BEGIN.format(slug=slug)
        end = _MARKER_END.format(slug=slug)

        # --- per-slug host-based conf: rewritten wholesale, always current
        (NGINX_DIR / f"{slug}.conf").write_text(f"""server {{
    listen 80;
    server_name {host};

    location / {{
        resolver 127.0.0.11 valid=5s ipv6=off;
        set $upstream http://{app_ip}:{port};
        proxy_pass $upstream;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Prefix /;
    }}
}}
""")

        # --- shared default.conf: replace this slug's marked region in place
        default_conf_path = NGINX_DIR / "default.conf"
        conf = default_conf_path.read_text() if default_conf_path.exists() else DEFAULT_CONF_SEED
        block = self._location_block(slug, app_ip, port)

        pattern = re.compile(
            r"\n?[ \t]*" + re.escape(begin) + r".*?" + re.escape(end) + r"[ \t]*\n?",
            re.DOTALL,
        )
        if pattern.search(conf):
            conf = pattern.sub(block, conf, count=1)
        else:
            # Insert before the final closing brace of the server block.
            if "}" not in conf:
                raise DeployError(500, {
                    "stage": "routing",
                    "error": f"{default_conf_path} contains no closing brace; cannot insert a location block",
                    "hint": "Restore it from the seed in orchestrator.DEFAULT_CONF_SEED",
                })
            conf = conf.rstrip()
            conf = conf[: conf.rfind("}")] + block + "}\n"
        default_conf_path.write_text(conf)

    def reload_nginx(self) -> None:
        """Reload the nginx container if one is running. Absent is not an error."""
        try:
            nginx_container = self.client.containers.get("helios-nginx")
        except NotFound:
            return
        result = nginx_container.exec_run("nginx -t")
        if getattr(result, "exit_code", 1) != 0:
            output = (getattr(result, "output", b"") or b"").decode(errors="replace")
            raise DeployError(500, {
                "stage": "routing",
                "error": "generated nginx configuration failed `nginx -t`",
                "logs": output[-4000:],
                "hint": "The previous configuration was left in place; nginx was not reloaded.",
            })
        nginx_container.exec_run("nginx -s reload")

    # ------------------------------------------------------------ health
    def _container_health_ok(self, container: Container, path: str, port: int,
                             attempts: int = 30) -> tuple[bool, str]:
        """Poll a container's own HTTP endpoint from inside it.

        Used for the rollback gate: the candidate app must answer on the tenant
        network before anything live is removed.
        """
        script = (
            "import urllib.request,sys;"
            f"sys.exit(0 if urllib.request.urlopen("
            f"'http://127.0.0.1:{port}{path}',timeout=2).status==200 else 1)"
        )
        logs = ""
        for _ in range(attempts):
            try:
                container.reload()
            except (APIError, NotFound):
                break
            if container.status in ("exited", "dead"):
                logs = self._safe_logs(container)
                break
            try:
                if container.exec_run(["python", "-c", script]).exit_code == 0:
                    return True, ""
            except APIError:
                pass
            time.sleep(1)
            logs = self._safe_logs(container)
        return False, logs

    def _safe_logs(self, container: Container, tail: int = 200) -> str:
        try:
            return container.logs(stdout=True, stderr=True, tail=tail).decode(errors="replace")[-4000:]
        except (APIError, NotFound):
            return ""

    # ------------------------------------------------------------ volumes
    def _core_volume(self, tenant: str) -> str:
        """Named volume holding the tenant core's ledger, graph and cache."""
        return f"helios-data-{tenant}-core"

    def _data_volume(self, tenant: str, app: str) -> str:
        """Named volume holding action logs, audit chain, holds and replay state."""
        return f"helios-data-{tenant}-{app}"

    # ------------------------------------------------------------ pipeline
    def deploy(self, tenant: str, manifest: Manifest, app_dir: Path) -> dict:
        base_info = self.ensure_base_images()
        t = sanitize(tenant or manifest.tenant or "demo")
        a = manifest.name
        port = manifest.port
        net, state = self.ensure_tenant(t)
        ingress = self._network(INGRESS_NET, internal=False)
        slug = f"{a}-{t}"
        host = f"{slug}.helios.test"

        image_tag = f"helios-app-{t}-{a}:latest"
        volume = self._data_volume(t, a)
        data_dir = manifest.data_dir
        volumes = {volume: {"bind": data_dir, "mode": "rw"}}

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

        # --- 3. sandbox smoke test: no Redis, no auditor, no key, no data volume.
        # An app whose /health needs any of those is not deployable, because the
        # smoke test is deliberately isolated.
        action_token = uuid.uuid4().hex
        sandbox = self._network(SANDBOX_NET, internal=False)
        smoke_name = f"smoke-{t}-{a}-{uuid.uuid4().hex[:6]}"
        smoke_env = {
            "HELIOS_APP_NAME": a, "HELIOS_TENANT": t,
            "HELIOS_ACTION_TOKEN": action_token,
            "HELIOS_AUDIT_URL": "http://helios-audit-smoke:8080/execute",
            "HELIOS_REDIS_URL": "redis://helios-redis:6379/0",
            "HELIOS_DATA_DIR": data_dir,
        }
        smoke = self.client.containers.create(
            image_tag, name=smoke_name, network=sandbox.name, environment=smoke_env)
        smoke.start()
        healthy, _ = self._container_health_ok(smoke, "/health", port, attempts=45)
        if not healthy:
            logs = self._safe_logs(smoke)
            try:
                smoke.remove(force=True)
            except (APIError, NotFound):
                pass
            raise DeployError(422, {"stage": "smoke_test",
                                    "error": "container never passed /health (crash logs below)",
                                    "logs": logs})
        try:
            smoke.remove(force=True)
        except (APIError, NotFound):
            pass

        # --- 4+5. blue/green attachment.
        # Candidates come up under -next names with -next aliases. Nothing that
        # is currently serving is touched until the candidate answers /health.
        audit_name = f"{t}-{a}-audit"
        app_name = f"{t}-{a}-app"
        audit_next = f"{audit_name}-next"
        app_next = f"{app_name}-next"

        # The app's audit manifest is copied out of the bundle into the auditor
        # image's policy dir at container level, so the app cannot modify its own
        # policy after deploy. Read it here, while the bundle is still on disk.
        audit_manifest_text = ""
        if manifest.audit_manifest:
            candidate = app_dir / manifest.audit_manifest
            if not candidate.is_file():
                raise DeployError(422, {
                    "stage": "manifest",
                    "error": (
                        f"audit_manifest '{manifest.audit_manifest}' is not present "
                        f"in the bundle"
                    ),
                    "hint": (
                        f"Include that file in the bundle, or remove the "
                        f"audit_manifest field from helios.yaml"
                    ),
                })
            audit_manifest_text = candidate.read_text(encoding="utf-8")

        app_env = {
            "HELIOS_APP_NAME": a, "HELIOS_TENANT": t,
            "HELIOS_ACTION_TOKEN": action_token,
            "HELIOS_AUDIT_URL": f"http://{a}-audit:8080/execute",
            "HELIOS_REDIS_URL": "redis://helios-redis:6379/0",
            "HELIOS_DATA_DIR": data_dir,
        }
        # Names only in the manifest; values resolved from the engine's own
        # environment and never returned or logged.
        app_env.update(resolve_env(manifest))

        audit_env = {
            "HELIOS_APP_NAME": a, "HELIOS_TENANT": t,
            "EXPECTED_ACTION_TOKEN": action_token,
            "HELIOS_EXECUTION_SECRET": state["execution_secret"],
            "HELIOS_CORE_URL": "http://helios-core:9000",
            "ALLOWED_TOOLS": manifest.allowed_tools,
            "HELIOS_DATA_DIR": data_dir,
            "HELIOS_AUDIT_MANIFEST_PATH": "/policy/audit.yaml",
            "HELIOS_AUDIT_MANIFEST_INLINE": "1" if audit_manifest_text else "0",
        }
        # HELIOS_ENGINE_URL was injected by the imported build and never read by
        # the auditor. Dropped rather than wired: a dead, secret-adjacent env var
        # in a sidecar is a liability, not a feature. See specs/decisions.md.

        labels_next = {"helios.managed": "true", "helios.role": "candidate",
                       "helios.tenant": t, "helios.app": a, "helios.data_dir": data_dir}

        candidates: list[Container] = []
        try:
            # Created STOPPED on purpose. The sidecar reads its policy manifest
            # during startup; a file injected after the process is up would be
            # ignored, leaving the platform's last gate running with no rules.
            audit_candidate = self._container(
                audit_next, AUDIT_IMAGE,
                {net.name: {"aliases": [f"{a}-audit-next"]}, ingress.name: {}},
                audit_env, labels=labels_next, replace=False,
                volumes=volumes,
                extra_hosts={"host.docker.internal": "host-gateway"},
                start=False,
            )
            candidates.append(audit_candidate)
            if audit_manifest_text:
                self._inject_policy(audit_candidate, audit_manifest_text)
            audit_candidate.start()

            app_candidate = self._container(
                app_next, image_tag,
                {net.name: {"aliases": [f"{a}-next"]}, ingress.name: {}},
                app_env, labels=labels_next, replace=False,
                volumes=volumes,
            )
            candidates.append(app_candidate)

            # Gate: both candidates must be healthy before the old pair is removed.
            ok, logs = self._container_health_ok(audit_candidate, "/health", 8080, attempts=30)
            if not ok:
                raise DeployError(422, {
                    "stage": "smoke_test",
                    "error": "audit sidecar candidate never passed /health; existing deployment left running",
                    "logs": logs,
                })
            ok, logs = self._container_health_ok(app_candidate, "/health", port, attempts=30)
            if not ok:
                raise DeployError(422, {
                    "stage": "smoke_test",
                    "error": "app candidate never passed /health on the tenant network; existing deployment left running",
                    "logs": logs,
                })
        except Exception:
            for c in candidates:
                try:
                    c.remove(force=True)
                except (APIError, NotFound):
                    pass
            raise

        # --- swap. Past this point the old pair is gone; the candidates are
        # known-good, so the remaining steps are naming and routing.
        self._remove_if_exists(audit_name)
        self._remove_if_exists(app_name)

        self._promote(audit_candidate, audit_name, net.name, f"{a}-audit", t, a)
        self._promote(app_candidate, app_name, net.name, a, t, a)

        # The IP is read to prove the container is attached to the ingress
        # network (it raises if not), but the route is written by NAME so a
        # restart, which reassigns the IP, does not strand the route.
        self._network_ip(app_candidate, ingress.name)
        self.write_nginx_route(slug, host, app_name, port)
        self.reload_nginx()

        return {
            "status": "deployed",
            "tenant": t,
            "app": a,
            "image": image_tag,
            "base_image": base_info,
            "containers": {"app": app_name, "audit_sidecar": audit_name},
            "internal_dns": {"app": f"http://{a}:{port}", "audit": f"http://{a}-audit:8080"},
            "urls": [f"http://{host}", f"http://localhost/apps/{slug}/"],
            # Names only. Values are resolved from the engine environment and
            # are deliberately never echoed back.
            "audit": {
                "allowed_tools": list(manifest.permissions),
                "env_forwarded": list(manifest.env),
                "audit_manifest": manifest.audit_manifest or None,
                "data_dir": manifest.data_dir,
                "data_volume": volume,
            },
        }

    # ------------------------------------------------------------ swap helpers
    def _remove_if_exists(self, name: str) -> None:
        try:
            self.client.containers.get(name).remove(force=True)
        except (NotFound, APIError):
            pass

    def _promote(self, container: Container, final_name: str, network_name: str,
                 alias: str, tenant: str, app: str) -> None:
        """Rename a candidate to its production name and give it the live alias.

        Network aliases are not renamed by `container.rename`, so the candidate
        is disconnected and reconnected with the production alias. Until this
        runs, nothing resolves the app's real DNS name to the candidate -- which
        is exactly what keeps a failed candidate from receiving traffic.

        Labels cannot be changed on a running container, so the candidate keeps
        `helios.role=candidate` and `list_apps()` reports that role honestly
        alongside the production name. Readers should treat role as "how this
        container was created", not "whether it is serving".
        """
        try:
            network = self.client.networks.get(network_name)
        except (NotFound, APIError):
            network = None
        if network is not None:
            try:
                network.disconnect(container, force=True)
            except (APIError, NotFound):
                pass
        try:
            container.rename(final_name)
        except APIError as exc:
            raise DeployError(500, {
                "stage": "swap",
                "error": f"could not rename candidate '{container.name}' to '{final_name}': {exc}",
                "hint": "The previous deployment has already been removed; re-run the deploy.",
            }) from exc
        if network is not None:
            try:
                network.connect(container, aliases=[alias])
            except APIError:
                # Best effort: name resolution still works via container name.
                pass
        try:
            container.reload()
        except (APIError, NotFound):
            pass

    def _inject_policy(self, container: Container, manifest_text: str) -> None:
        """Place the app's audit manifest into the sidecar at /policy/audit.yaml.

        Written into the container before it is started. The app itself has no
        volume or exec access to this path, so it cannot rewrite its own policy
        after deploy -- the property that makes the sidecar a platform gate
        rather than an app convenience.
        """
        import io
        import tarfile

        buf = io.BytesIO()
        data = manifest_text.encode("utf-8")
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo("audit.yaml")
            info.size = len(data)
            info.mode = 0o444  # read-only inside the container
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        try:
            container.put_archive("/policy", buf.getvalue())
        except APIError as exc:
            raise DeployError(500, {
                "stage": "audit_policy",
                "error": f"could not install the audit manifest into '{container.name}': {exc}",
                "hint": "The sidecar would run with a default deny policy; refusing to continue.",
            }) from exc

    # ------------------------------------------------------------- queries
    def list_apps(self) -> list[dict]:
        out = []
        for c in self.client.containers.list(all=True, filters={"label": "helios.managed=true"}):
            role = c.labels.get("helios.role")
            if role not in {"app", "candidate"}:
                continue
            app = c.labels.get("helios.app")
            tenant = c.labels.get("helios.tenant")
            slug = f"{app}-{tenant}"
            out.append({
                "container": c.name, "status": c.status, "role": role,
                "tenant": tenant, "app": app,
                "image": (c.image.tags or ["<none>"])[0],
                "url": f"http://localhost/apps/{slug}/",
            })
        return sorted(out, key=lambda x: x["container"])

    def logs(self, container_name: str, tail: int = 200) -> list[str]:
        try:
            raw = self.client.containers.get(container_name).logs(
                stdout=True, stderr=True, tail=tail).decode(errors="replace")
            return [line for line in raw.splitlines() if line.strip()]
        except NotFound:
            raise DeployError(404, f"no such container: {container_name}")

    def undeploy(self, tenant: str, app: str | None = None) -> dict:
        """Remove deployed containers (and optionally their data volume).

        Only containers carrying the helios.managed label for this tenant are
        touched. The data volume is preserved unless explicitly requested,
        because it holds the hash-chained audit log -- deleting it would destroy
        compliance evidence, which is not a side effect of "undeploy the app".
        """
        t = sanitize(tenant)
        removed: list[str] = []
        removed_apps: set[str] = set()
        for c in self.client.containers.list(all=True, filters={"label": "helios.managed=true"}):
            if c.labels.get("helios.tenant") != t:
                continue
            if app and c.labels.get("helios.app") != sanitize(app):
                continue
            try:
                c.remove(force=True)
                removed.append(c.name)
                if c.labels.get("helios.app"):
                    removed_apps.add(c.labels["helios.app"])
            except (APIError, NotFound):
                continue
        # Every slug this call took down loses its route, whether one app was
        # named or the whole tenant was. A route left behind proxies to a dead
        # IP and answers 502 until the next deploy of that slug.
        slugs = sorted({f"{sanitize(app)}-{t}"} if app else
                       {f"{name}-{t}" for name in removed_apps})
        for slug in slugs:
            path = NGINX_DIR / f"{slug}.conf"
            if path.exists():
                path.unlink()
            self._strip_nginx_block(slug)
        if slugs:
            try:
                self.reload_nginx()
            except DeployError:
                pass
        return {"status": "undeployed", "tenant": t, "app": app, "removed": sorted(removed),
                "routes_removed": slugs, "data_volumes_kept": True}

    # ------------------------------------------------------------ evidence
    def tenant_logs(self, tenant: str, app: str | None = None, tail: int = 200) -> dict:
        """Every app's action log, audit chain and sidecar log, read from the volumes.

        The engine runs on the host and only the containers see the named
        volumes, so each log is pulled through `get_archive` from a container
        that mounts it. Hash-chained files are verified with `audit_agent`'s own
        chain walker, so the number reported here is the same one the sidecar's
        /health reports.
        """
        t = sanitize(tenant)
        wanted = sanitize(app) if app else None
        apps: dict[str, dict] = {}
        for c in self.client.containers.list(all=True, filters={"label": "helios.managed=true"}):
            labels = c.labels or {}
            if labels.get("helios.tenant") != t:
                continue
            name = labels.get("helios.app")
            role = labels.get("helios.role")
            if not name or role not in ("app", "candidate", "running"):
                continue
            if wanted and name != wanted:
                continue
            data_dir = labels.get("helios.data_dir") or "/data"
            entry = apps.setdefault(name, {"container": c.name, "data_dir": data_dir, "files": []})
            try:
                stream, _ = c.get_archive(data_dir)
            except (APIError, NotFound) as exc:
                entry["error"] = f"could not read {data_dir}: {exc}"
                continue
            entry["files"] = _jsonl_files_from_tar(stream, tail)
        return {"tenant": t, "apps": apps}

    def _strip_nginx_block(self, slug: str) -> None:
        default_conf_path = NGINX_DIR / "default.conf"
        if not default_conf_path.exists():
            return
        conf = default_conf_path.read_text()
        pattern = re.compile(
            r"\n?[ \t]*" + re.escape(_MARKER_BEGIN.format(slug=slug)) + r".*?"
            + re.escape(_MARKER_END.format(slug=slug)) + r"[ \t]*\n?",
            re.DOTALL,
        )
        default_conf_path.write_text(pattern.sub("", conf, count=1))

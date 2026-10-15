"""Helios Automatic Deployment Engine — FastAPI control plane over docker-py.

Error contract
--------------
Every rejection is a 422 (or the status named by `DeployError`) with a JSON body
that FastAPI wraps under a top-level `detail` key. Inside it:

    {"stage": "manifest|static_audit|build|smoke_test|routing|execution",
     "error": "<one-line summary>",
     "problems": ["<each field, its problem, what to write instead>", ...],
     "hint": "<optional next action>",
     "logs": "<last 4000 chars, build/smoke only>"}

`problems` is plural and complete on purpose: an author fixing a manifest should
not redeploy four times to find four errors. The usual author is Qwen, so each
entry names the field, the problem, and the replacement.

No import-time side effects
---------------------------
The imported build created its runtime directory at import, which meant merely
importing this module wrote to disk -- a test hazard, and it wrote next to the
engine rather than next to the repository. Directories are created on demand,
and `RUN` comes from the orchestrator so both modules agree on one location.
"""
from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

from .contracts import MANIFEST_NAME, ContractError, lint, load_manifest
from .orchestrator import RUN, DeployError, Orchestrator

app = FastAPI(title="Helios Deployment Engine", version="0.2.0")
_orch: Orchestrator | None = None


def orch() -> Orchestrator:
    """Lazily connect to Docker.

    Deferred so that importing this module -- and therefore running tests that
    only exercise manifest validation -- never requires a live daemon.
    """
    global _orch
    if _orch is None:
        try:
            _orch = Orchestrator()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
    return _orch


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract a bundle, refusing any member that escapes the destination.

    A zip from an untrusted app is attacker-controlled input: without this check
    a `../../` member would write outside the work directory (zip-slip), which in
    this engine means overwriting platform source that the base image embeds.
    """
    dest = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        if target != dest and not str(target).startswith(str(dest) + "/"):
            raise HTTPException(422, {
                "stage": "manifest",
                "error": f"unsafe path in bundle: {member.filename}",
                "hint": "Bundle paths must be relative and must not contain '..'. "
                        "Re-zip the app directory from inside it.",
            })
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def _find_app_root(src: Path) -> Path:
    """Locate helios.yaml at the bundle root, or one wrapper folder down.

    One level of wrapper is tolerated because `zip -r app.zip my-app/` is the
    natural thing to type and produces exactly that shape.
    """
    if (src / MANIFEST_NAME).is_file():
        return src
    dirs = [p for p in src.iterdir() if p.is_dir()]
    for d in dirs:
        if (d / MANIFEST_NAME).is_file():
            return d
    for d in dirs:
        for sub in (p for p in d.iterdir() if p.is_dir()):
            if (sub / MANIFEST_NAME).is_file():
                return sub
    raise HTTPException(422, {
        "stage": "manifest",
        "error": f"no {MANIFEST_NAME} found at the bundle root",
        "hint": "Put helios.yaml at the top level of the zip, or inside a single "
                "wrapper folder. Deeper nesting is not searched.",
    })


@app.get("/health")
def health():
    """Liveness only. Does not require a Docker daemon."""
    return {"status": "ok", "service": "helios-deployment-engine", "version": app.version}


@app.get("/api/v1/doctor")
def doctor():
    """Diagnostic snapshot: daemon, engine state, base image, nginx, run dir."""
    report: dict = {"engine": "ok", "run_dir": str(RUN)}
    try:
        o = orch()
    except HTTPException as exc:
        return JSONResponse({"engine": "ok", "docker": "unreachable",
                             "detail": exc.detail}, status_code=200)

    try:
        info = o.client.info()
        report["docker"] = {
            "server_version": info.get("ServerVersion"),
            "containers_running": info.get("ContainersRunning"),
            "images": info.get("Images"),
        }
    except Exception as exc:
        report["docker"] = {"error": str(exc)}

    from .orchestrator import platform_hash
    report["platform_hash"] = platform_hash()
    try:
        image = o.client.images.get(f"helios-app-base:{report['platform_hash']}")
        report["base_image"] = {"present": True, "tags": list(image.tags)}
    except Exception:
        report["base_image"] = {
            "present": False,
            "hint": "It will be built on the next deploy.",
        }

    try:
        o.client.containers.get("helios-nginx")
        report["nginx"] = "running"
    except Exception:
        report["nginx"] = "not running"

    try:
        report["apps"] = o.list_apps()
    except Exception as exc:
        report["apps"] = {"error": str(exc)}
    return report


@app.post("/api/v1/deploy")
async def deploy(bundle: UploadFile = File(...), tenant: str = Form("")):
    RUN.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="deploy-", dir=str(RUN)))
    try:
        zp = work / "bundle.zip"
        zp.write_bytes(await bundle.read())
        try:
            zf = zipfile.ZipFile(zp)
        except zipfile.BadZipFile as exc:
            raise HTTPException(422, {
                "stage": "manifest",
                "error": "uploaded bundle is not a valid zip",
                "hint": "Create it with: zip -r app.zip . (from inside the app dir)",
            }) from exc
        src = work / "src"
        src.mkdir()
        with zf:
            _safe_extract(zf, src)

        app_dir = _find_app_root(src)  # step 1: ingest

        try:  # contract validation -- reports every problem at once
            manifest = load_manifest(app_dir)
        except ContractError as exc:
            raise HTTPException(422, {
                "stage": "manifest",
                "error": f"{MANIFEST_NAME} failed validation",
                "problems": exc.problems,
            }) from exc

        problems = lint(app_dir)  # step 1: static audit (no hardcoded keys)
        if problems:
            raise HTTPException(422, {"stage": "static_audit", "problems": problems,
                                      "hint": "remove credentials from code; Helios injects "
                                              "runtime secrets at deploy time"})

        effective_tenant = tenant or manifest.tenant or "demo"
        try:  # steps 2-5
            return orch().deploy(effective_tenant, manifest, app_dir)
        except DeployError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.get("/api/v1/apps")
def apps():
    return {"apps": orch().list_apps()}


@app.get("/api/v1/apps/{container_name}/logs")
def app_logs(container_name: str, tail: int = Query(200, ge=1, le=10000)):
    try:
        return {"container": container_name, "logs": orch().logs(container_name, tail)}
    except DeployError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@app.get("/api/v1/tenants/{tenant}/logs")
def tenant_logs(tenant: str, app_name: str = Query("", alias="app"),
                tail: int = Query(200, ge=1, le=10000)):
    """Central evidence: every app's action log, audit chain and sidecar log.

    Read from the named data volumes through the running containers, with
    each hash chain verified by audit_agent's own walker.
    """
    try:
        return orch().tenant_logs(tenant, app_name or None, tail)
    except DeployError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@app.delete("/api/v1/apps/{name}")
def undeploy(name: str, tenant: str = Query("")):
    """Remove a deployed app, or every app of one tenant.

    `name` is resolved in this order, so the CLI can pass whatever the operator
    typed without guessing which kind of identifier it is:

      1. an exact container name (`mytenant-myapp-app`)
      2. an app id (`myapp`), scoped to `tenant` when given, otherwise every
         tenant running that app
      3. a tenant id (`mytenant`), removing all of that tenant's apps

    Only containers carrying `helios.managed=true` are ever touched.

    Data volumes are NOT removed. A volume holds the hash-chained audit log,
    which is compliance evidence; destroying it is not an implied consequence of
    "undeploy the app", and it is not recoverable. Removing one must be a
    separate, explicit act (docker volume rm), never a side effect here.
    """
    o = orch()

    managed = [
        c for c in o.client.containers.list(all=True, filters={"label": "helios.managed=true"})
    ]
    if not managed:
        raise HTTPException(404, {
            "stage": "undeploy",
            "error": f"nothing deployed matches '{name}'",
            "hint": "List what is deployed with GET /api/v1/apps",
        })

    target_app = target_tenant = None
    exact = next((c for c in managed if c.name == name), None)
    if exact is not None:
        target_app = exact.labels.get("helios.app")
        target_tenant = exact.labels.get("helios.tenant")
    elif tenant:
        target_app, target_tenant = name, tenant
    else:
        app_names = {c.labels.get("helios.app") for c in managed}
        tenant_names = {c.labels.get("helios.tenant") for c in managed}
        if name in app_names:
            target_app = name
        elif name in tenant_names:
            target_tenant = name
        else:
            raise HTTPException(404, {
                "stage": "undeploy",
                "error": (
                    f"'{name}' is not a deployed container, app id or tenant id"
                ),
                "hint": (
                    "Known apps: "
                    + ", ".join(sorted(x for x in app_names if x))
                    + "; known tenants: "
                    + ", ".join(sorted(x for x in tenant_names if x))
                ),
            })

    try:
        if target_tenant and not target_app:
            result = o.undeploy(target_tenant)
        else:
            result = o.undeploy(target_tenant or "demo", target_app)
        return result
    except DeployError as exc:
        raise HTTPException(exc.status, exc.detail) from exc

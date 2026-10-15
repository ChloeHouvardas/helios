"""Helios Automatic Deployment Engine — FastAPI control plane over docker-py."""
from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from .contracts import ContractError, MANIFEST_NAME, lint, load_manifest
from .orchestrator import DeployError, Orchestrator

RUN = Path(__file__).resolve().parent.parent / ".helios-run"
RUN.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Helios Deployment Engine", version="0.1.0")
_orch: Orchestrator | None = None


def orch() -> Orchestrator:
    global _orch
    if _orch is None:
        try:
            _orch = Orchestrator()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
    return _orch


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        if target != dest and not str(target).startswith(str(dest) + "/"):
            raise HTTPException(422, {"error": f"unsafe path in bundle: {member.filename}"})
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def _find_app_root(src: Path) -> Path:
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
    raise HTTPException(422, {"error": f"no {MANIFEST_NAME} found at the bundle root "
                                       "(one top-level wrapper folder is allowed)"})


@app.get("/health")
def health():
    return {"status": "ok", "service": "helios-deployment-engine"}


@app.post("/api/v1/deploy")
async def deploy(bundle: UploadFile = File(...), tenant: str = Form("")):
    work = Path(tempfile.mkdtemp(prefix="deploy-", dir=str(RUN)))
    try:
        zp = work / "bundle.zip"
        zp.write_bytes(await bundle.read())
        try:
            zf = zipfile.ZipFile(zp)
        except zipfile.BadZipFile as exc:
            raise HTTPException(422, {"error": "uploaded bundle is not a valid zip"}) from exc
        src = work / "src"
        src.mkdir()
        with zf:
            _safe_extract(zf, src)

        app_dir = _find_app_root(src)  # step 1: ingest

        try:  # contract validation
            manifest = load_manifest(app_dir)
        except ContractError as exc:
            raise HTTPException(422, {"stage": "manifest", "error": str(exc)}) from exc

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
def app_logs(container_name: str, tail: int = 200):
    try:
        return {"container": container_name, "logs": orch().logs(container_name, tail)}
    except DeployError as exc:
        raise HTTPException(exc.status, exc.detail) from exc

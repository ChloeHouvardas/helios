"""Helios app contract: helios.yaml manifest schema + static ingest lint."""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

MANIFEST_NAME = "helios.yaml"
ALLOWED_TOOLS = {"payments", "email", "knowledge-graph", "events"}
TEXT_EXT = {".py", ".js", ".ts", ".json", ".yaml", ".yml", ".env", ".txt", ".sh", ".cfg", ".ini", ".toml"}

SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "OpenAI-style API key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"ghp_[A-Za-z0-9]{36}"), "GitHub personal access token"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "Slack token"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "private key"),
]


class ContractError(ValueError):
    pass


class Manifest(BaseModel):
    name: str
    tenant: str = ""
    entry: str = "main:app"
    port: int = 3000
    permissions: list[str] = []


def sanitize(value: str) -> str:
    s = re.sub(r"[^a-z0-9\-]", "-", value.lower()).strip("-")
    s = re.sub(r"^[^a-z0-9]+", "", s)
    return s or "app"


def load_manifest(app_dir: Path) -> Manifest:
    path = app_dir / MANIFEST_NAME
    if not path.is_file():
        raise ContractError(f"missing required file: {MANIFEST_NAME} at the bundle root")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ContractError(f"{MANIFEST_NAME} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError(f"{MANIFEST_NAME} must be a mapping")
    try:
        manifest = Manifest(**data)
    except ValidationError as exc:
        raise ContractError(f"{MANIFEST_NAME} schema error: {exc}") from exc
    manifest.name = sanitize(manifest.name)
    manifest.tenant = sanitize(manifest.tenant) if manifest.tenant else ""
    unknown = [p for p in manifest.permissions if p not in ALLOWED_TOOLS]
    if unknown:
        raise ContractError(
            f"{MANIFEST_NAME}: unknown permissions {unknown}; allowed: {sorted(ALLOWED_TOOLS)}"
        )
    if manifest.port < 1 or manifest.port > 65535:
        raise ContractError(f"{MANIFEST_NAME}: invalid port {manifest.port}")
    return manifest


def lint(app_dir: Path) -> list[str]:
    """Return human-readable static-analysis problems (hardcoded credentials, etc.)."""
    problems: list[str] = []
    for path in sorted(app_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXT:
            continue
        if any(part in {".git", "__pycache__", ".venv", "node_modules"} for part in path.parts):
            continue
        try:
            if path.stat().st_size > 1_000_000:
                continue
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(app_dir)
        for pattern, label in SECRET_PATTERNS:
            if pattern.search(text):
                problems.append(f"{rel}: possible hardcoded {label}")
    return problems

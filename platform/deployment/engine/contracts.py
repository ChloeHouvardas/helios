"""Helios app contract: helios.yaml manifest schema + static ingest lint.

Two rules shape everything in this module.

**Validation reports all problems at once.** An author fixing a manifest should
not have to redeploy four times to discover four errors. `load_manifest`
collects every problem into one message rather than raising on the first.

**Error messages are written for a model to act on.** Each names the field, the
problem, and what to write instead. The author of a generated app is usually
Qwen, and a vague message costs a full generation round-trip.

Fields are optional with backwards-compatible defaults, so a manifest written
before `env`/`audit_manifest`/`data_dir` existed still loads unchanged.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

MANIFEST_NAME = "helios.yaml"

#: Tool families an app may request. Each grants the audit sidecar the right to
#: forward that action to the tenant core service. An app asking for a
#: permission it does not use is a review smell, not an error -- but an app
#: asking for one that does not exist is a typo that must fail loudly, because
#: silently dropping it would leave the app unable to act at runtime with no
#: explanation.
ALLOWED_TOOLS = {
    "payments",
    "email",
    "knowledge-graph",
    "events",
    # Added by the p2-deploy contract extension.
    "forecast",
    "market-data",
    "spreadsheets",
    "collect",
}

#: An app that declares no permissions is granted NO platform family. The
#: imported build fell back to payments + email, which handed a read-only app
#: the right to move money and send mail it never asked for. The sidecar still
#: accepts tools the app's own audit manifest declares, so such an app can act
#: on its own data; it just cannot reach the tenant core.
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = ()

TEXT_EXT = {".py", ".js", ".ts", ".json", ".yaml", ".yml", ".env", ".txt", ".sh", ".cfg", ".ini", ".toml"}

SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "OpenAI-style API key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"ghp_[A-Za-z0-9]{36}"), "GitHub personal access token"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "Slack token"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "private key"),
]

#: Environment variable names the engine is willing to forward from its own
#: environment into an app container. The prefix allow-list exists because an app
#: author should not be able to ask for `AWS_SECRET_ACCESS_KEY`, `SSH_AUTH_SOCK`
#: or any other host credential by naming it.
#:
#: Only NAMES cross this boundary, never values: the manifest says "pass
#: DASHSCOPE_API_KEY if the engine has it", and the value is resolved from the
#: engine's own environment at container-creation time. It is never written to
#: the manifest, the deploy response, or a log line.
ENV_NAME_PREFIX = re.compile(r"^(HELIOS_|DASHSCOPE_|OPENAI_|TUSHARE_|EMAILTOOL_)")

#: A name must be a valid POSIX environment variable name. Rejecting anything
#: else avoids handing Docker a malformed `environment=` entry.
ENV_NAME_SHAPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Where the engine mounts the per-app data volume by default.
DEFAULT_DATA_DIR = "/data"


class ContractError(ValueError):
    """A manifest or bundle failed validation. Carries every problem found."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__(self.render())

    def render(self) -> str:
        if len(self.problems) == 1:
            return self.problems[0]
        lines = "\n".join(f"  - {p}" for p in self.problems)
        return f"{len(self.problems)} problems in {MANIFEST_NAME}:\n{lines}"


class Manifest(BaseModel):
    """The deployment manifest: everything the engine needs to run an app.

    Distinct from `helios-app.yaml`, which is the app-builder manifest and
    describes the agents. The two have different schemas; neither replaces the
    other, and an app dir may legitimately contain both.
    """

    #: App id. Sanitised to [a-z0-9-] because it becomes part of a container
    #: name, a DNS alias and a URL path segment.
    name: str
    tenant: str = ""
    #: Uvicorn target inside the bundle, e.g. `server:app`.
    entry: str = "main:app"
    port: int = 3000
    #: Requested tool families. Subset of ALLOWED_TOOLS.
    permissions: list[str] = Field(default_factory=list)

    # --- p2-deploy extensions (all optional, all backwards compatible) -------

    #: Environment variable NAMES the engine forwards from its own environment
    #: into the app container. Names only; values never appear in the manifest,
    #: in a deploy response, or in a log line.
    env: list[str] = Field(default_factory=list)

    #: Path, inside the bundle, of the app's YAML audit manifest. The engine
    #: copies it into the audit sidecar at /policy/audit.yaml, where the app can
    #: no longer modify it. Without this the sidecar falls back to a default
    #: deny-all-but-core policy.
    audit_manifest: str = ""

    #: Mount point for the named data volume. Action logs, hash-chained audit
    #: logs, SQLite holds and replay guards live here so they survive a
    #: redeploy. Must be absolute: a relative path would resolve against the
    #: container's WORKDIR and silently land somewhere else.
    data_dir: str = DEFAULT_DATA_DIR

    @property
    def allowed_tools(self) -> str:
        """Comma-separated family list for the auditor's ALLOWED_TOOLS.

        Empty when the manifest grants nothing: no family is ever implied.
        """
        return ",".join(self.permissions)


def sanitize(value: str) -> str:
    """Coerce an arbitrary string into a safe [a-z0-9-] identifier."""
    s = re.sub(r"[^a-z0-9\-]", "-", value.lower()).strip("-")
    s = re.sub(r"^[^a-z0-9]+", "", s)
    return s or "app"


def _validate_env_names(raw: Any, problems: list[str]) -> list[str]:
    """Check `env` is a list of acceptable variable NAMES.

    Returns the validated, de-duplicated name list. Every rejection says what
    to write instead, because the usual cause is an author putting a
    `NAME=value` pair or an unprefixed name in this field.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        problems.append(
            f"{MANIFEST_NAME}: 'env' must be a list of variable NAMES, got "
            f"{type(raw).__name__}. Write: env: [DASHSCOPE_API_KEY]"
        )
        return []

    names: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            problems.append(
                f"{MANIFEST_NAME}: 'env' entry {item!r} is not a string. Write "
                f"the variable name alone, e.g. env: [DASHSCOPE_API_KEY]"
            )
            continue
        name = item.strip()
        if not name:
            problems.append(f"{MANIFEST_NAME}: 'env' contains an empty entry; remove it")
            continue
        if "=" in name:
            problems.append(
                f"{MANIFEST_NAME}: 'env' entry '{name}' assigns a value. This "
                f"field takes NAMES only -- the engine supplies the value from "
                f"its own environment. Write: {name.split('=', 1)[0]}"
            )
            continue
        if not ENV_NAME_SHAPE.match(name):
            problems.append(
                f"{MANIFEST_NAME}: 'env' entry '{name}' is not a valid variable "
                f"name. Use letters, digits and underscores, starting with a "
                f"letter or underscore."
            )
            continue
        if not ENV_NAME_PREFIX.match(name):
            problems.append(
                f"{MANIFEST_NAME}: 'env' entry '{name}' is not on the forward "
                f"allow-list. Only names starting with HELIOS_, DASHSCOPE_, "
                f"OPENAI_, TUSHARE_ or EMAILTOOL_ may be passed into an app "
                f"container. Secret material is injected by the platform, not "
                f"requested by the app."
            )
            continue
        if name not in names:
            names.append(name)
    return names


def _validate_data_dir(raw: Any, problems: list[str]) -> str:
    """Check `data_dir` is an absolute, sane mount point."""
    if raw is None or raw == "":
        return DEFAULT_DATA_DIR
    if not isinstance(raw, str):
        problems.append(
            f"{MANIFEST_NAME}: 'data_dir' must be a string path, got "
            f"{type(raw).__name__}. Write: data_dir: /data"
        )
        return DEFAULT_DATA_DIR
    value = raw.strip()
    if not value.startswith("/"):
        problems.append(
            f"{MANIFEST_NAME}: 'data_dir' must be absolute, got '{value}'. A "
            f"relative path would resolve against the container WORKDIR and "
            f"land somewhere unpredictable. Write: data_dir: /data"
        )
        return DEFAULT_DATA_DIR
    if value == "/":
        problems.append(
            f"{MANIFEST_NAME}: 'data_dir' cannot be '/'. That would mount the "
            f"data volume over the container's entire filesystem and hide the "
            f"application. Write: data_dir: /data"
        )
        return DEFAULT_DATA_DIR
    # Reject traversal so a manifest cannot ask for a mount outside the volume.
    parts = [p for p in value.split("/") if p]
    if any(p == ".." for p in parts):
        problems.append(
            f"{MANIFEST_NAME}: 'data_dir' cannot contain '..' (got '{value}'). "
            f"Write an absolute path with no traversal, e.g. data_dir: /data"
        )
        return DEFAULT_DATA_DIR
    return "/" + "/".join(parts)


def _validate_audit_manifest(raw: Any, problems: list[str]) -> str:
    """Check `audit_manifest` is a relative, in-bundle path."""
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str):
        problems.append(
            f"{MANIFEST_NAME}: 'audit_manifest' must be a string path, got "
            f"{type(raw).__name__}. Write: audit_manifest: agents/my-app-audit.yaml"
        )
        return ""
    value = raw.strip()
    if value.startswith("/"):
        problems.append(
            f"{MANIFEST_NAME}: 'audit_manifest' must be relative to the bundle "
            f"root, got absolute '{value}'. The engine reads it from the "
            f"uploaded bundle, not from the host. Write: "
            f"audit_manifest: agents/my-app-audit.yaml"
        )
        return ""
    if any(p == ".." for p in value.split("/")):
        problems.append(
            f"{MANIFEST_NAME}: 'audit_manifest' cannot contain '..' (got "
            f"'{value}'). It must stay inside the bundle."
        )
        return ""
    if Path(value).suffix.lower() not in {".yaml", ".yml", ".json"}:
        problems.append(
            f"{MANIFEST_NAME}: 'audit_manifest' must point at a .yaml, .yml or "
            f".json file, got '{value}'. Write: "
            f"audit_manifest: agents/my-app-audit.yaml"
        )
        return ""
    return value


def load_manifest(app_dir: Path) -> Manifest:
    """Parse and validate `helios.yaml`, collecting every problem before raising.

    Fail closed: anything unreadable, wrong-shaped or out of range is rejected
    with the full list of problems. There is no permissive fallback.
    """
    path = app_dir / MANIFEST_NAME
    problems: list[str] = []

    if not path.is_file():
        raise ContractError([
            f"missing required file: {MANIFEST_NAME} at the bundle root. "
            f"Create it with at least: name: my-app"
        ])
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ContractError([f"{MANIFEST_NAME} is not valid YAML: {exc}"]) from exc
    except OSError as exc:
        raise ContractError([f"{MANIFEST_NAME} could not be read: {exc}"]) from exc

    if not isinstance(data, dict):
        raise ContractError([
            f"{MANIFEST_NAME} must be a mapping of fields, got "
            f"{type(data).__name__}. Write: name: my-app"
        ])

    known = set(Manifest.model_fields)
    unexpected = sorted(set(data) - known)
    if unexpected:
        problems.append(
            f"{MANIFEST_NAME}: unknown field(s) {unexpected}. Known fields: "
            f"{sorted(known)}. A misspelled field is silently ignored by a "
            f"lenient parser, so it is rejected here."
        )

    # --- name (required) ----------------------------------------------------
    raw_name = data.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        problems.append(
            f"{MANIFEST_NAME}: 'name' is required and must be a non-empty "
            f"string. Write: name: my-app"
        )
        name = "app"
    else:
        name = sanitize(raw_name)

    tenant_raw = data.get("tenant", "")
    tenant = sanitize(tenant_raw) if isinstance(tenant_raw, str) and tenant_raw.strip() else ""
    if tenant_raw and not isinstance(tenant_raw, str):
        problems.append(
            f"{MANIFEST_NAME}: 'tenant' must be a string, got "
            f"{type(tenant_raw).__name__}"
        )

    # --- entry --------------------------------------------------------------
    entry = data.get("entry", "main:app")
    if not isinstance(entry, str) or ":" not in entry:
        problems.append(
            f"{MANIFEST_NAME}: 'entry' must be 'module:attribute', got "
            f"{entry!r}. Write: entry: server:app"
        )
        entry = "main:app"

    # --- port ---------------------------------------------------------------
    port = data.get("port", 3000)
    if isinstance(port, bool) or not isinstance(port, int):
        problems.append(
            f"{MANIFEST_NAME}: 'port' must be an integer, got {port!r} "
            f"({type(port).__name__}). Write: port: 3000"
        )
        port = 3000
    elif not 1 <= port <= 65535:
        problems.append(
            f"{MANIFEST_NAME}: 'port' {port} is outside 1-65535. Write: port: 3000"
        )
        port = 3000

    # --- permissions --------------------------------------------------------
    raw_perms = data.get("permissions", [])
    permissions: list[str] = []
    if raw_perms is None:
        raw_perms = []
    if not isinstance(raw_perms, list):
        problems.append(
            f"{MANIFEST_NAME}: 'permissions' must be a list, got "
            f"{type(raw_perms).__name__}. Write: permissions: [payments, email]"
        )
    else:
        for item in raw_perms:
            if not isinstance(item, str):
                problems.append(
                    f"{MANIFEST_NAME}: 'permissions' entry {item!r} is not a "
                    f"string. Allowed values: {sorted(ALLOWED_TOOLS)}"
                )
                continue
            if item not in ALLOWED_TOOLS:
                problems.append(
                    f"{MANIFEST_NAME}: unknown permission '{item}'. Allowed: "
                    f"{sorted(ALLOWED_TOOLS)}. Remove it or use the closest "
                    f"match -- an unrecognised permission is never granted "
                    f"silently."
                )
                continue
            if item not in permissions:
                permissions.append(item)

    # --- extension fields ---------------------------------------------------
    env = _validate_env_names(data.get("env"), problems)
    data_dir = _validate_data_dir(data.get("data_dir"), problems)
    audit_manifest = _validate_audit_manifest(data.get("audit_manifest"), problems)

    if problems:
        raise ContractError(problems)

    return Manifest(
        name=name,
        tenant=tenant,
        entry=entry,
        port=port,
        permissions=permissions,
        env=env,
        audit_manifest=audit_manifest,
        data_dir=data_dir,
    )


def resolve_env(manifest: Manifest, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Turn a manifest's env NAME allow-list into actual NAME=value pairs.

    The values come from the engine's own environment. A name the engine does
    not have is skipped rather than injected as an empty string, so an app can
    tell "not configured" from "configured as empty".

    This is the only place env values are materialised. Callers must not log
    the result; the deploy response reports names only.
    """
    source = os.environ if environ is None else environ
    return {name: source[name] for name in manifest.env if name in source}


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


__all__ = [
    "ALLOWED_TOOLS",
    "ContractError",
    "DEFAULT_ALLOWED_TOOLS",
    "DEFAULT_DATA_DIR",
    "ENV_NAME_PREFIX",
    "MANIFEST_NAME",
    "Manifest",
    "SECRET_PATTERNS",
    "TEXT_EXT",
    "lint",
    "load_manifest",
    "resolve_env",
    "sanitize",
]

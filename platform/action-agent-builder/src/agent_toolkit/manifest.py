"""The agent manifest: the format Qwen fills in to create a new agent
from data alone.

A manifest is plain JSON. Loading is strict: unknown fields, missing
required fields, bad types, or a mandate referencing tools the manifest
does not offer are all rejected with an error naming the problem — never
silently ignored. A manifest alone fully determines an agent: its
instructions, its tools, and its mandate.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .models import Mandate

ALLOWED_FIELDS = frozenset(
    {"name", "version", "description", "instructions", "tools", "mandate", "model", "max_turns"}
)
REQUIRED_FIELDS = frozenset({"name", "description", "instructions", "tools", "mandate"})
_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DEFAULT_MAX_TURNS = 12


class ManifestError(ValueError):
    """Raised when a manifest is missing, unreadable, or invalid."""


@dataclass(frozen=True)
class AgentManifest:
    name: str
    description: str
    instructions: str
    tools: tuple[str, ...]
    mandate: Mandate
    version: str = "1"
    model: str | None = None
    max_turns: int = DEFAULT_MAX_TURNS


def validate_manifest(data: Mapping[str, Any]) -> AgentManifest:
    if not isinstance(data, Mapping):
        raise ManifestError("manifest must be a JSON object")

    unknown = sorted(set(data) - ALLOWED_FIELDS)
    if unknown:
        raise ManifestError(f"unknown manifest field(s): {unknown}; allowed: {sorted(ALLOWED_FIELDS)}")
    missing = sorted(REQUIRED_FIELDS - set(data))
    if missing:
        raise ManifestError(f"missing required manifest field(s): {missing}")

    name = data["name"]
    if not isinstance(name, str) or not _NAME_PATTERN.match(name):
        raise ManifestError(
            "manifest name must be kebab-case (lowercase letters, digits, hyphens), "
            f"got: {name!r}"
        )

    description = data["description"]
    if not isinstance(description, str) or not description.strip():
        raise ManifestError("manifest description must be a non-empty string")

    instructions = data["instructions"]
    if not isinstance(instructions, str) or not instructions.strip():
        raise ManifestError("manifest instructions must be a non-empty string")

    tools = data["tools"]
    if (
        not isinstance(tools, (list, tuple))
        or not tools
        or not all(isinstance(tool, str) and tool.strip() for tool in tools)
    ):
        raise ManifestError("manifest tools must be a non-empty list of tool names")
    if len(set(tools)) != len(tools):
        raise ManifestError("manifest tools contains duplicates")

    version = data.get("version", "1")
    if not isinstance(version, str) or not version.strip():
        raise ManifestError("manifest version must be a non-empty string")

    model = data.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ManifestError("manifest model must be a non-empty string or null")

    max_turns = data.get("max_turns", DEFAULT_MAX_TURNS)
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
        raise ManifestError("manifest max_turns must be an integer >= 1")

    try:
        mandate = Mandate.from_dict(data["mandate"])
    except ValueError as error:
        raise ManifestError(f"manifest mandate: {error}") from error

    unoffered = sorted(set(mandate.allowed_tools) - set(tools))
    if unoffered:
        raise ManifestError(
            f"mandate allows tool(s) the manifest does not offer: {unoffered}; "
            "add them to manifest tools or remove them from the mandate"
        )

    return AgentManifest(
        name=name,
        description=description,
        instructions=instructions,
        tools=tuple(tools),
        mandate=mandate,
        version=version,
        model=model,
        max_turns=max_turns,
    )


def load_manifest(path: Path | str) -> AgentManifest:
    path = Path(path)
    if not path.exists():
        raise ManifestError(f"manifest file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ManifestError(f"manifest {path} is not valid JSON: {error}") from error
    try:
        return validate_manifest(data)
    except ManifestError as error:
        raise ManifestError(f"manifest {path}: {error}") from error

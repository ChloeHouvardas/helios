"""Discover the deployment engine's accepted manifest fields AT GENERATION TIME.

`helios.yaml` is consumed by `platform/deployment/`, and the engine's pydantic
model does NOT set `extra="forbid"` — unknown keys are silently dropped. That is
the worst possible failure mode for a deployment contract: emitting a `data_dir`
the engine ignores produces a manifest that *looks* configured and is not, with no
error anywhere.

So the scaffolder does not hard-code a field list. It reads the engine's
`contracts.py` and emits only what that model actually declares. When the engine
gains a field, generated apps start emitting it; when a field is removed, they stop.

Today `platform/deployment/` does not exist in the repo — the engine is only in the
read-only `OTHER-TEAMMATE-STUFF/` copy — so this falls back to the field set
recorded from that copy on 2026-10-15 and says so in the generated file.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

__all__ = [
    "ManifestFields", "discover_manifest_fields", "DEFAULT_FIELDS",
    "DEFAULT_ALLOWED_TOOLS",
]

#: Recorded from OTHER-TEAMMATE-STUFF/.../helios/engine/contracts.py on
#: 2026-10-15, because platform/deployment/ does not exist yet. Used only when the
#: engine cannot be read.
DEFAULT_FIELDS: tuple[str, ...] = ("name", "tenant", "entry", "port", "permissions")

#: `ALLOWED_TOOLS` from the same file. `load_manifest` raises `ContractError` on
#: anything else, so an unknown permission makes the app undeployable.
DEFAULT_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {"payments", "email", "knowledge-graph", "events"})

#: Where the engine is expected to land, relative to the repo root.
_ENGINE_CONTRACTS = Path("platform") / "deployment" / "engine" / "contracts.py"


class ManifestFields:
    """What the deployment engine's manifest accepts, and where that came from."""

    def __init__(self, fields: tuple[str, ...], allowed_tools: frozenset[str],
                 *, source: str, discovered: bool) -> None:
        self.fields = fields
        self.allowed_tools = allowed_tools
        #: Human-readable provenance, written into the generated file as a comment.
        self.source = source
        #: True when read from the engine itself rather than from the fallback.
        self.discovered = discovered

    def accepts(self, key: str) -> bool:
        return key in self.fields


def discover_manifest_fields(repo_root: Path | str | None = None) -> ManifestFields:
    """Read the engine's `Manifest` model and `ALLOWED_TOOLS` if they exist.

    Falls back to the recorded field set, and says so, rather than guessing. A
    guess would emit fields the engine drops on the floor.
    """
    root = Path(repo_root) if repo_root is not None else _guess_repo_root()
    path = root / _ENGINE_CONTRACTS
    if not path.is_file():
        return ManifestFields(
            DEFAULT_FIELDS, DEFAULT_ALLOWED_TOOLS,
            source=(f"platform/deployment/engine/contracts.py was not found, so the "
                    f"field set recorded from the engine's read-only copy on "
                    f"2026-10-15 is used: {', '.join(DEFAULT_FIELDS)}"),
            discovered=False,
        )

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return ManifestFields(
            DEFAULT_FIELDS, DEFAULT_ALLOWED_TOOLS,
            source=(f"{path} could not be parsed ({exc}); using the recorded "
                    f"field set: {', '.join(DEFAULT_FIELDS)}"),
            discovered=False,
        )

    fields = _manifest_fields(tree)
    allowed = _allowed_tools(tree)

    if not fields:
        return ManifestFields(
            DEFAULT_FIELDS, allowed or DEFAULT_ALLOWED_TOOLS,
            source=(f"no Manifest model found in {path}; using the recorded field "
                    f"set: {', '.join(DEFAULT_FIELDS)}"),
            discovered=False,
        )

    return ManifestFields(
        tuple(fields),
        allowed or DEFAULT_ALLOWED_TOOLS,
        source=f"read from {path}",
        discovered=True,
    )


def _manifest_fields(tree: ast.Module) -> list[str]:
    """Annotated attribute names of the model named `Manifest` (or `*Manifest`)."""
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "Manifest" and not node.name.endswith("Manifest"):
            continue
        names: list[str] = []
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                names.append(stmt.target.id)
        if names:
            return names
    return []


def _allowed_tools(tree: ast.Module) -> frozenset[str]:
    """The `ALLOWED_TOOLS` set literal, if the module defines one."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "ALLOWED_TOOLS":
                values = _literal_strings(node.value)
                if values:
                    return frozenset(values)
    return frozenset()


def _literal_strings(node: Any) -> list[str]:
    """String members of a set/list/tuple/frozenset literal."""
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return [e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in ("frozenset", "set", "list", "tuple") and node.args):
        return _literal_strings(node.args[0])
    return []


def _guess_repo_root() -> Path:
    """Walk up from this file to the directory that holds `platform/`."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "platform").is_dir() and (parent / "specs").is_dir():
            return parent
    return here.parents[4] if len(here.parents) > 4 else here.parent

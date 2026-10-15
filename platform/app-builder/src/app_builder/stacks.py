"""Load `stacks.yaml`: the named stacks and the type map the schema tool uses.

Kept separate from `schema.py` so that "which stack" and "how to build it" stay
independent. Adding a stack means adding an entry to the YAML; changing how DDL is
emitted means changing `schema.py`. Neither should require touching the other.

Versions in `stacks.yaml` are pinned rather than floating, because an app generated
in week one has to still build in week ten and "latest" is not a version.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "Stack", "StacksError", "load_stacks", "default_stack", "stack_for_spec",
    "PACKAGE_DIR", "STACKS_YAML", "kit_dir",
]

#: The installed package directory. Everything shipped with app-builder is found
#: relative to this, so paths work both in a source checkout and from a wheel.
PACKAGE_DIR = Path(__file__).resolve().parent

#: `stacks.yaml` lives at `platform/app-builder/stacks.yaml` in the repo — outside
#: the package, where a reader can find it — and is copied INTO the wheel next to
#: this module by hatchling's force-include. Both locations are tried, package dir
#: first, because in an installed wheel the repo layout does not exist at all.
#:
#: An editable install masks this class of bug entirely: the source tree is the
#: install, so a path that only works in the repo appears to work everywhere.
_STACKS_CANDIDATES = (
    PACKAGE_DIR / "stacks.yaml",
    PACKAGE_DIR.parents[1] / "stacks.yaml",
)


class StacksError(ValueError):
    """`stacks.yaml` is missing or malformed.

    Raised rather than defaulted: guessing a stack would generate an app against a
    database the user did not choose.
    """


def _find_stacks_yaml() -> Path:
    for candidate in _STACKS_CANDIDATES:
        if candidate.exists():
            return candidate
    tried = ", ".join(str(c) for c in _STACKS_CANDIDATES)
    raise StacksError(
        f"no stacks.yaml found (looked in {tried}); it ships with app-builder and "
        "names the available stacks and their type maps")


#: Resolved at import. A missing stacks.yaml means a broken install, and failing
#: here says so once, clearly, instead of as a confusing error three calls later.
STACKS_YAML = _find_stacks_yaml()


def kit_dir() -> Path:
    """The UI kit directory: Jinja2 templates, CSS tokens and vendored htmx."""
    return PACKAGE_DIR / "kit"


@lru_cache(maxsize=4)
def load_stacks(path: str | Path | None = None) -> dict[str, Any]:
    """Parse `stacks.yaml`. Cached — the file does not change within a run."""
    p = Path(path) if path is not None else STACKS_YAML
    if not p.exists():
        raise StacksError(
            f"no stacks.yaml at {p}; it ships with app-builder and names the "
            "available stacks and their type maps")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise StacksError(f"{p} does not parse as YAML: {' '.join(str(exc).split())}") from exc
    if not isinstance(data, dict) or "stacks" not in data:
        raise StacksError(f"{p} must be a mapping with a `stacks` key")
    return data


class Stack:
    """One named stack, with the type map and CHECK fragments it needs."""

    def __init__(self, name: str, definition: dict[str, Any],
                 checks: dict[str, str]) -> None:
        self.name = name
        self.definition = definition
        self._checks = checks

    @property
    def type_map(self) -> dict[str, Any]:
        return self.definition.get("type_map") or {}

    @property
    def database(self) -> str:
        return str((self.definition.get("database") or {}).get("name", ""))

    @property
    def language(self) -> str:
        lang = self.definition.get("language") or {}
        return f"{lang.get('name', '')} {lang.get('version', '')}".strip()

    @property
    def web_framework(self) -> str:
        fw = self.definition.get("web_framework") or {}
        return f"{fw.get('name', '')} {fw.get('version', '')}".strip()

    @property
    def frontend(self) -> dict[str, Any]:
        return self.definition.get("frontend") or {}

    def sql_type(self, field_type: str) -> str:
        entry = self.type_map.get(field_type)
        if not entry:
            raise StacksError(
                f"stack {self.name!r} has no type_map entry for field type "
                f"{field_type!r}; write one of {sorted(self.type_map)}")
        return str(entry["sql_type"])

    def check_for(self, field_type: str) -> str | None:
        """The raw SQL CHECK fragment for a field type, or None."""
        entry = self.type_map.get(field_type) or {}
        key = entry.get("check")
        return self._checks.get(key) if key else None

    def requires_json_valid(self) -> bool:
        """True when any CHECK uses json_valid, so the caller can say so."""
        return any("json_valid" in v for v in self._checks.values())


def default_stack(path: str | Path | None = None) -> Stack:
    """The stack used when a spec names none."""
    data = load_stacks(path)
    name = data.get("default")
    if not name:
        raise StacksError("stacks.yaml has no `default` stack; add one")
    return _build(name, data)


def stack_for_spec(spec: Any, path: str | Path | None = None) -> Stack:
    """Resolve a spec's `stack` section to a `Stack`.

    `match_existing` has no entry in `stacks.yaml` — it means "adopt whatever the
    existing system uses", which is not a pinned stack this tool can generate DDL
    for. The caller decides what to do; here it is an error rather than a silent
    fallback to the default, because generating SQLite tables for an app joining a
    Postgres system would be wrong in a way that looks fine until it runs.
    """
    data = load_stacks(path)
    if spec.stack.match_existing:
        raise StacksError(
            "stack.match_existing adopts the existing system's stack, which has no "
            "entry in stacks.yaml and therefore no type map to generate DDL from. "
            "Generate against a named stack (`stack.use`) instead, or write the "
            "data layer by hand for the existing database.")
    name = spec.stack.use
    if name not in (data.get("stacks") or {}):
        available = sorted((data.get("stacks") or {}).keys())
        raise StacksError(
            f"stack.use names {name!r}, which stacks.yaml does not define; write "
            f"one of {available}")
    return _build(name, data)


def _build(name: str, data: dict[str, Any]) -> Stack:
    definition = (data.get("stacks") or {}).get(name)
    if not isinstance(definition, dict):
        raise StacksError(f"stack {name!r} is not a mapping in stacks.yaml")
    checks = data.get("checks") or {}
    if not isinstance(checks, dict):
        raise StacksError("`checks` in stacks.yaml must be a mapping")
    return Stack(name, definition, checks)

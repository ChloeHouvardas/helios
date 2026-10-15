"""Helios Application Builder.

Turns one `helios-app.yaml` spec into a complete, validator-clean application
skeleton: both agent manifests, the fact source, the tools, the SQLite schema,
the server, and the tests.

Module map
----------
`spec`        the pydantic models for the spec, and `AppSpecError`
`validate`    spec + manifests + app code, cross-checked
`schema`      `data.local` -> SQLite DDL and a generated `store.py`
`scaffold`    spec -> application skeleton on disk
`kit`         Jinja2 templates, CSS tokens, and the vendored HTMX

The package deliberately does NOT import `audit_agent` or `agent_toolkit` at
module scope. `validate` imports them lazily, inside the function that needs
them, so that a spec can be checked for shape without the sibling packages being
installed. The scaffolder never imports them at all: it reads the manifests as
plain data and generates an app that runs its own copy of the runtime.
"""
from __future__ import annotations

from .spec import (
    AppSpec,
    AppSpecError,
    FIELD_TYPES,
    FieldType,
    SPEC_VERSION,
    UI_PATTERNS,
    UiPattern,
    looks_monetary,
)

__version__ = "0.1.0"

__all__ = [
    "AppSpec",
    "AppSpecError",
    "FIELD_TYPES",
    "FieldType",
    "SPEC_VERSION",
    "UI_PATTERNS",
    "UiPattern",
    "looks_monetary",
    "__version__",
]

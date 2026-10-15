"""Email sender for the Helios email tool."""

from .idempotency import DuplicateKeyError, IdempotencyStore, derive_key
from .templates import RenderedEmail, TemplateError, TemplateRegistry

__all__ = [
    "DuplicateKeyError",
    "IdempotencyStore",
    "RenderedEmail",
    "TemplateError",
    "TemplateRegistry",
    "derive_key",
]

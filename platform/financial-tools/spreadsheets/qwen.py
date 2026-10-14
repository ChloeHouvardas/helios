"""Qwen client for the three places the guide calls for model inference.

Used for: inferring column mappings on `import_sheet`, turning plain-language
column descriptions into formulas for `render_sheet`, and suggesting matches
`reconcile` rules cannot settle.

Competition rule: **no hard-coded API keys**. Credentials come from the
environment / Helios vault only, and never appear in logs — every request and
response passes through `redact_for_logs`.

Qwen is optional. When no endpoint is configured, callers degrade to
deterministic behaviour and report that inference was unavailable rather than
guessing.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .errors import HeliosError, upstream_unavailable
from .filestore import redact_for_logs

# Env vars read at call time, never imported as module constants holding secrets.
BASE_URL_VAR = "QWEN_BASE_URL"
API_KEY_VAR = "QWEN_API_KEY"
MODEL_VAR = "QWEN_MODEL"
DEFAULT_MODEL = "qwen-max"

# Any OpenAI-compatible chat endpoint. The guide notes a spreadsheet product's
# formula assistant can accept such an endpoint, so it points at Qwen.
CHAT_PATH = "/chat/completions"


class QwenUnavailable(HeliosError):
    """Raised when inference is asked for but no endpoint is configured."""

    def __init__(self, detail: str = "no Qwen endpoint configured"):
        super().__init__("upstream_unavailable",
                         f"Qwen inference unavailable: {detail}",
                         retryable=False)


class QwenClient:
    """Thin OpenAI-compatible client with graceful degradation."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: float = 30.0):
        self.base_url = (base_url or os.environ.get(BASE_URL_VAR, "")).rstrip("/")
        self.api_key = api_key or os.environ.get(API_KEY_VAR, "")
        self.model = model or os.environ.get(MODEL_VAR, DEFAULT_MODEL)
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def complete(self, system: str, user: str,
                 json_mode: bool = False) -> str:
        """One chat completion. Raises upstream_unavailable on transport failure."""
        if not self.configured:
            raise QwenUnavailable()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise upstream_unavailable("Qwen (httpx not installed)") from exc
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    self.base_url + CHAT_PATH,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            # Never echo credentials or request bodies into the error.
            raise upstream_unavailable("Qwen") from exc
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise upstream_unavailable("Qwen (malformed response)") from exc

    def complete_json(self, system: str, user: str) -> Any:
        """A completion parsed as JSON; raises validation_error if unparseable."""
        raw = self.complete(system, user, json_mode=True)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # Tolerate fenced blocks, which models sometimes emit.
            text = (raw or "").strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.startswith("json"):
                    text = text[4:]
            try:
                return json.loads(text.strip())
            except Exception as exc:
                raise HeliosError(
                    "upstream_unavailable",
                    "Qwen returned a response that was not valid JSON",
                    retryable=True) from exc


class NullQwen(QwenClient):
    """Explicit no-inference client for tests and offline development.

    Every call raises QwenUnavailable so callers take their deterministic path.
    """

    def __init__(self) -> None:
        super().__init__(base_url="", api_key="", model="null")

    @property
    def configured(self) -> bool:  # noqa: D102
        return False

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        raise QwenUnavailable("NullQwen")


def log_safe(payload: Any) -> Any:
    """Strip credentials before anything is logged or audited."""
    return redact_for_logs(payload)

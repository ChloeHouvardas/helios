"""The model driver: Qwen proposes, code still decides.

Kept in its own module so importing the runtime never imports an HTTP client
or a model SDK. Everything else in this package runs offline against
``ScriptedDriver``; this is the only module that needs a token.

Provider
--------
Qwen over DashScope's OpenAI-compatible Chat Completion endpoint. Competition
rule: Qwen is the only generative model allowed here. Three details of that
contract are load-bearing:

- Auth is a **plain Bearer token**. Nothing here signs or encodes it; it is
  sent exactly as given.
- ``enable_thinking`` is a **DashScope extension**, not an OpenAI field. It is
  sent as a top-level body parameter (what the OpenAI SDK calls
  ``extra_body``), and only for hybrid reasoning models — identified here by a
  ``qwen3`` model id. Those models default to thinking mode and reject a
  non-streaming request while it is on, so it is turned off explicitly. A
  non-hybrid model must not be sent the field at all: an endpoint that does not
  know the parameter rejects an unknown one, and an endpoint that does would
  change behaviour for a model that never had it.
- ``tool_choice`` is sent as ``"auto"``, and ``stream`` stays ``False`` so one
  request is one complete reply — the runtime adjudicates each side effect
  before the next turn, which a streaming parse would complicate for no gain.

Stdlib HTTP only, matching the house style of ``audit_agent.transport``: no
SDK to install, no transitive dependency tree, and the request is visible in
one function.

Security
--------
The token is read from the environment and used for exactly one header. It is
never logged, never written to disk, never put in an exception message, and
never sent anywhere except the configured base URL.

Why the runtime sees each call *before* it executes
---------------------------------------------------
An off-the-shelf agent helper runs the tool loop for you, which would execute
side effects without passing them through the audit bridge — exactly what this
platform exists to prevent. So this driver returns a proposal and stops.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Mapping, Sequence

from agent_toolkit.contracts import ToolDefinition

from .runtime import ToolCall

#: Defaults, each overridable by environment. The base URL is a variable
#: rather than a constant because a competition or enterprise gateway will
#: issue its own endpoint, and a token shaped like a gateway token will not
#: authenticate against the public one.
DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3.8-max"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_SECONDS = 120.0

#: Accepted names for the token, in priority order. ``HELIOS_LLM_API_KEY`` is
#: this repo's own name and always wins. ``DASHSCOPE_API_KEY`` is the vendor's
#: name. ``OPENAI_API_KEY`` is last because Qwen Code sets ``OPENAI_API_KEY``
#: and ``OPENAI_BASE_URL`` in the environment, and honouring them means a
#: developer who has already configured Qwen Code needs no second setup — but
#: an explicitly Helios or DashScope key must still take precedence.
TOKEN_ENV_VARS: tuple[str, ...] = (
    "HELIOS_LLM_API_KEY",
    "DASHSCOPE_API_KEY",
    "OPENAI_API_KEY",
)

#: Environment fallbacks for the base URL, after ``HELIOS_LLM_BASE_URL``.
#: ``OPENAI_BASE_URL`` is honoured for the same reason as ``OPENAI_API_KEY``.
BASE_URL_ENV_VARS: tuple[str, ...] = (
    "HELIOS_LLM_BASE_URL",
    "OPENAI_BASE_URL",
)

#: Model ids that are hybrid reasoning models and therefore need
#: ``enable_thinking`` sent explicitly. Matched as a substring of the model id,
#: lowercased.
HYBRID_THINKING_PREFIX = "qwen3"


def _is_hybrid_thinking_model(model: str) -> bool:
    """True for a model that has a thinking mode to switch off.

    The qwen3 family are hybrid: thinking is on by default, and a non-streaming
    request with it on is rejected. Everything else must not be sent the field
    at all — see the module docstring.
    """
    return HYBRID_THINKING_PREFIX in model.lower()

#: What the model is told about the gate. Worth including: an agent that
#: understands its denials adjusts; one that does not just retries.
GATE_NOTICE = (
    "Every call to a tool that changes the real world is reviewed by an "
    "independent audit agent before it runs. You cannot bypass it, and you "
    "cannot argue it out of a denial. Rules:\n"
    "- Call exactly ONE tool per turn.\n"
    "- If a call is DENIED you receive the reason. Read it and change your "
    "approach; do not retry the same call with the same arguments.\n"
    "- If a call is HELD, a human owner must decide. Stop and report; do not "
    "retry and do not work around it.\n"
    "- If the auditor grants fewer arguments than you asked for, the granted "
    "value is what executes.\n"
    "- Tools you were not granted do not exist. Do not invent tool names.\n"
    "When the task is finished, reply with plain text and call no tool."
)


class ModelError(RuntimeError):
    """The model could not be reached or answered usefully.

    Deliberately a plain ``RuntimeError`` subclass so the runtime's existing
    "a broken model is an error, not an allow" handling catches it.
    """


def resolve_token() -> str:
    """The first configured token, or an empty string."""
    for name in TOKEN_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def token_env_var_name() -> str:
    """Which variable supplied the token — safe to print, unlike its value."""
    for name in TOKEN_ENV_VARS:
        if os.environ.get(name, "").strip():
            return name
    return TOKEN_ENV_VARS[0]


def api_key_present() -> bool:
    """True when some model token is configured."""
    return bool(resolve_token())


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class ModelDriver:
    """A ``Driver`` backed by an OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        token: str | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.model = (
            model
            or _env("HELIOS_LLM_MODEL", "")
            or _env("OPENAI_MODEL", "")
            or DEFAULT_MODEL
        )
        self.base_url = (
            base_url
            or _env(BASE_URL_ENV_VARS[0], "")
            or _env(BASE_URL_ENV_VARS[1], "")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self.max_tokens = max_tokens or _env_int(
            "HELIOS_LLM_MAX_TOKENS", DEFAULT_MAX_TOKENS
        )
        self.timeout_seconds = timeout_seconds
        # Read once, held in memory only. Never logged, never serialised.
        self._token = token if token is not None else resolve_token()

    # -- Driver ------------------------------------------------------------

    def next_calls(
        self,
        *,
        task: str,
        instructions: str,
        tools: Sequence[ToolDefinition],
        history: Sequence[Mapping[str, Any]],
    ) -> list[ToolCall] | str:
        """Ask the model what to do next.

        ``tools`` is already narrowed to the auditor's grant, so an ungranted
        tool is not merely refused — the model never learns it exists.
        """
        if not self._token:
            raise ModelError(
                f"no model token is set. Copy .env.example to .env and set "
                f"{TOKEN_ENV_VARS[0]} (or {TOKEN_ENV_VARS[1]}), or run with "
                "--offline to use the scripted driver."
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(instructions, history),
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if _is_hybrid_thinking_model(self.model):
            # A qwen3 hybrid model defaults to thinking mode and refuses a
            # non-streaming request while it is on, so turn it off explicitly.
            # For any other model id, send neither thinking field: an endpoint
            # that does not know the parameter rejects an unknown one.
            payload["enable_thinking"] = False
        functions = [tool_function(tool) for tool in tools]
        if functions:
            payload["tools"] = functions
            # "auto" is the only value this endpoint documents.
            payload["tool_choice"] = "auto"

        data = self._post("/chat/completions", payload)
        return self._parse(data, {f["function"]["name"] for f in functions})

    # -- Request -----------------------------------------------------------

    def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """One POST. Raises ModelError; never leaks the token."""
        url = f"{self.base_url}{path}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = _safe_error_body(exc)
            raise ModelError(
                f"the model endpoint returned HTTP {exc.code} for {self.model}. "
                f"{detail}"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Name the failure without echoing the request, which carries the
            # Authorization header.
            raise ModelError(
                f"the model endpoint could not be reached at {self.base_url}: "
                f"{type(exc).__name__}"
            ) from None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelError(f"the model endpoint returned invalid JSON: {exc}") from None
        if not isinstance(data, dict):
            raise ModelError("the model endpoint returned a non-object body")
        return data

    # -- Messages ----------------------------------------------------------

    def _messages(
        self, instructions: str, history: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        # GATE_NOTICE is always present, so there is always a system turn.
        system = "\n\n".join(
            part for part in (instructions.strip(), GATE_NOTICE) if part
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(as_chat_messages(history))
        # The endpoint refuses a thread of only system/assistant turns, and
        # that is reachable: history is empty on the very first turn if the
        # caller passes no task text. Add a user turn rather than fail.
        if all(m["role"] in {"system", "assistant"} for m in messages):
            messages.append({"role": "user", "content": "Continue the task."})
        return messages

    # -- Response ----------------------------------------------------------

    def _parse(self, data: Mapping[str, Any], known: set[str]) -> Any:
        if "error" in data:
            error = data.get("error") or {}
            code = error.get("code", "") if isinstance(error, Mapping) else ""
            message = (
                error.get("message", "") if isinstance(error, Mapping) else str(error)
            )
            raise ModelError(f"the model endpoint reported an error: {code} {message}")

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            finish = data.get("finish_reason", "")
            raise ModelError(
                f"the model returned no choices{f' (finish_reason={finish})' if finish else ''}"
            )
        message = choices[0].get("message") or {}

        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            call = _first_function_call(tool_calls)
            if call is not None:
                name = call["name"]
                if name not in known:
                    # Never execute a tool nobody registered. Surface it as
                    # text so the model can correct itself; the auditor would
                    # deny it anyway.
                    return (
                        f"You asked for tool '{name}', which is not available "
                        f"for this task. Available tools: {sorted(known)}. "
                        "Choose one of them, or finish in plain text."
                    )
                return [ToolCall(name=name, arguments=parse_arguments(call["arguments"]))]

        text = text_of(message)
        if text:
            return text
        # A response with neither a tool call nor text is useless to the loop.
        # Returning empty text would silently end the run as a success.
        raise ModelError(
            "the model returned neither a tool call nor any text "
            f"(finish_reason={choices[0].get('finish_reason', 'unknown')})"
        )


def tool_function(tool: ToolDefinition) -> dict[str, Any]:
    """One tool in the wire format the endpoint expects.

    ``ToolDefinition.to_spec()`` returns the flat Qwen-Agent shape
    (``name``/``description``/``parameters``). This endpoint wants each tool
    wrapped in ``{"type": "function", "function": {...}}``, and a missing
    ``parameters`` object is rejected — so an empty schema is supplied.
    """
    spec = tool.to_spec()
    return {
        "type": "function",
        "function": {
            "name": spec.get("name", tool.name),
            "description": spec.get("description", tool.description),
            "parameters": spec.get("parameters") or {"type": "object", "properties": {}},
        },
    }


def as_chat_messages(history: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Translate runtime history into wire messages.

    The runtime records a tool outcome as ``{"role": "tool", "name", "content"}``
    with no call id. This protocol pairs every tool result with an assistant
    ``tool_calls`` turn carrying a matching ``tool_call_id``, so the ids are
    synthesised positionally: the n-th tool result in history pairs with
    ``call_n``. That is consistent across rebuilds because history is
    append-only and the driver emits one tool call per turn.
    """
    out: list[dict[str, Any]] = []
    index = 0
    for entry in history:
        role = entry.get("role")
        content = entry.get("content", "")
        if role == "tool":
            call_id = f"call_{index}"
            index += 1
            name = str(entry.get("name") or f"tool_{index}")
            out.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                # The real arguments are not in the runtime's
                                # history. An empty object keeps the turn
                                # well-formed; the result content below is what
                                # the model actually reads.
                                "arguments": "{}",
                            },
                        }
                    ],
                }
            )
            out.append(
                {"role": "tool", "tool_call_id": call_id, "content": str(content)}
            )
        elif role in {"user", "assistant", "system"}:
            text = str(content) if content is not None else ""
            out.append({"role": str(role), "content": text})
    return out


def _first_function_call(tool_calls: Sequence[Any]) -> dict[str, str] | None:
    """The first well-formed function call, or None.

    Only one is honoured: the runtime adjudicates each side effect
    separately, and a batch of unreviewed calls would execute before the gate
    saw the second one.
    """
    for item in tool_calls:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") not in (None, "function"):
            continue  # web_search / retrieval / mcp are not our tools
        function = item.get("function")
        if not isinstance(function, Mapping):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = function.get("arguments")
        return {
            "name": name,
            "arguments": arguments if isinstance(arguments, str) else "",
        }
    return None


def parse_arguments(arguments: Any) -> dict[str, Any]:
    """Model arguments arrive as JSON text; malformed becomes an empty dict.

    An empty dict then fails the auditor's schema check and comes back as a
    deny with a reason — the correct fail-closed outcome. It is never quietly
    executed with whatever the model probably meant.
    """
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if not isinstance(arguments, str) or not arguments.strip():
        return {}
    try:
        loaded = json.loads(arguments)
    except json.JSONDecodeError:
        return {}
    return dict(loaded) if isinstance(loaded, Mapping) else {}


def text_of(message: Mapping[str, Any]) -> str:
    """The visible answer, ignoring the reasoning trace.

    A Qwen hybrid model returns ``reasoning_content`` alongside ``content``
    when it thinks. The trace is not the answer, so it is never treated as one
    — otherwise a run would end on the model's deliberation instead of its
    reply.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):  # multimodal content items
        parts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, Mapping)
        ]
        return "".join(parts).strip()
    return ""


def _safe_error_body(exc: urllib.error.HTTPError) -> str:
    """The server's error text, truncated and stripped of anything secret.

    Response bodies are quoted because a 401 from a gateway usually says which
    credential format it wanted — that is the single most useful debugging
    fact here. The request is never quoted, since it carries the token.
    """
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        text = raw.strip().replace("\n", " ")
        return text[:300]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return f"{error.get('code', '')} {error.get('message', '')}".strip()[:300]
        message = data.get("message") or data.get("msg")
        if isinstance(message, str):
            return message[:300]
    return json.dumps(data, ensure_ascii=False)[:300]


__all__ = [
    "BASE_URL_ENV_VARS",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "GATE_NOTICE",
    "ModelError",
    "ModelDriver",
    "TOKEN_ENV_VARS",
    "api_key_present",
    "as_chat_messages",
    "parse_arguments",
    "resolve_token",
    "text_of",
    "token_env_var_name",
    "tool_function",
]

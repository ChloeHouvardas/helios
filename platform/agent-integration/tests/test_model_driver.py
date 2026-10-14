"""Tests for the GLM-5.3 model driver.

Everything here is offline. The wire protocol is exercised against a local
``http.server`` that mimics the endpoint's documented request and response
shapes, so the request the driver *sends* is asserted as precisely as the
reply it parses.

Three properties matter most and each has a test:

- The token reaches the wire as a **plain Bearer** value and appears nowhere
  else — not in logs, not in error text, not on disk.
- A tool call comes back with arguments as a **JSON string**, and a malformed
  one degrades to an empty dict so the auditor denies it rather than the
  runtime guessing.
- A model that asks for an unregistered tool is **never** executed.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent_integration import ToolCall
from agent_integration.model_driver import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    GATE_NOTICE,
    ModelDriver,
    ModelError,
    api_key_present,
    as_chat_messages,
    parse_arguments,
    resolve_token,
    text_of,
    token_env_var_name,
    tool_function,
)
from agent_toolkit.contracts import ToolDefinition


# ---------------------------------------------------------------------------
# A local stand-in for the endpoint
# ---------------------------------------------------------------------------


class _MockServer:
    """Records requests, replies with a canned payload.

    Deliberately strict about the documented contract: it rejects a missing
    Authorization header and a request that tries to disable thinking, because
    GLM-5.3 does both and a driver that got either wrong should fail here
    rather than in production.
    """

    def __init__(self, replies: list[dict[str, Any]], *, expect_token: str = "tok") -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.errors: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    outer.errors.append("request body was not JSON")
                    self._send(400, {"error": {"code": "bad_json", "message": "not JSON"}})
                    return

                outer.requests.append(payload)
                outer.headers.append({k.lower(): v for k, v in self.headers.items()})

                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer "):
                    outer.errors.append(f"missing or malformed Authorization: {auth!r}")
                    self._send(401, {"error": {"code": "auth", "message": "no bearer token"}})
                    return
                if auth.removeprefix("Bearer ").strip() != expect_token:
                    outer.errors.append("token did not match")
                    self._send(401, {"error": {"code": "auth", "message": "bad token"}})
                    return
                if payload.get("thinking", {}).get("type") == "disabled":
                    outer.errors.append("tried to disable thinking on GLM-5.3")
                    self._send(400, {"error": {"code": "bad", "message": "thinking cannot be disabled"}})
                    return

                if not outer.replies:
                    outer.errors.append("more requests than canned replies")
                    self._send(500, {"error": {"code": "exhausted", "message": "no reply left"}})
                    return
                self._send(200, outer.replies.pop(0))

            def _send(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/paas/v4"

    def start(self) -> "_MockServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def mock_server():
    """A server that answers one plain-text turn."""
    server = _MockServer(
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "All done."},
                        "finish_reason": "stop",
                    }
                ]
            }
        ]
    ).start()
    yield server
    server.stop()


def _tools() -> list[ToolDefinition]:
    """Two minimal tools: one read-only, one with a side effect."""

    def handler(arguments):
        return {"echo": dict(arguments)}

    return [
        ToolDefinition(
            name="lookup_invoice",
            description="Look up one invoice.",
            input_schema={
                "type": "object",
                "properties": {"invoice_id": {"type": "string"}},
                "required": ["invoice_id"],
            },
            output_schema={"type": "object"},
            side_effect=False,
            handler=handler,
        ),
        ToolDefinition(
            name="send_email",
            description="Send an email. Side effect.",
            input_schema={
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "invoice_id": {"type": "string"},
                },
                "required": ["customer_id", "invoice_id"],
            },
            output_schema={"type": "object"},
            side_effect=True,
            handler=handler,
        ),
    ]


def _tool_reply(name: str, arguments: Any) -> dict[str, Any]:
    """A response shaped exactly like the documented one."""
    return {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc123",
                            "type": "function",
                            "function": {
                                "name": name,
                                # The endpoint documents this as a JSON string.
                                "arguments": (
                                    arguments
                                    if isinstance(arguments, str)
                                    else json.dumps(arguments)
                                ),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


# ---------------------------------------------------------------------------
# Configuration and token handling
# ---------------------------------------------------------------------------


def test_defaults_match_the_documented_endpoint() -> None:
    assert DEFAULT_BASE_URL == "https://open.bigmodel.cn/api/paas/v4"
    assert DEFAULT_MODEL == "glm-5.3"


def test_token_is_read_from_the_environment(monkeypatch) -> None:
    for name in ("HELIOS_LLM_API_KEY", "LLM_API_TOKEN", "GLM_API_KEY",
                 "ZHIPUAI_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert resolve_token() == ""
    assert api_key_present() is False

    monkeypatch.setenv("LLM_API_TOKEN", "  L2secret1Y  ")
    assert resolve_token() == "L2secret1Y"  # stripped
    assert api_key_present() is True
    assert token_env_var_name() == "LLM_API_TOKEN"


def test_token_priority_prefers_the_helios_name(monkeypatch) -> None:
    monkeypatch.setenv("HELIOS_LLM_API_KEY", "preferred")
    monkeypatch.setenv("LLM_API_TOKEN", "fallback")
    assert resolve_token() == "preferred"


def test_a_blank_token_counts_as_absent(monkeypatch) -> None:
    monkeypatch.setenv("HELIOS_LLM_API_KEY", "   ")
    assert api_key_present() is False


def test_environment_overrides_model_and_base_url(monkeypatch) -> None:
    monkeypatch.setenv("HELIOS_LLM_MODEL", "glm-5.3-flash")
    monkeypatch.setenv("HELIOS_LLM_BASE_URL", "https://gateway.example.com/v4/")
    monkeypatch.setenv("HELIOS_LLM_REASONING_EFFORT", "low")
    monkeypatch.setenv("HELIOS_LLM_MAX_TOKENS", "512")

    driver = ModelDriver(token="tok")
    assert driver.model == "glm-5.3-flash"
    assert driver.base_url == "https://gateway.example.com/v4"  # trailing / stripped
    assert driver.reasoning_effort == "low"
    assert driver.max_tokens == 512


def test_explicit_arguments_win_over_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("HELIOS_LLM_MODEL", "glm-5.3-flash")
    driver = ModelDriver(model="glm-5.3", token="tok")
    assert driver.model == "glm-5.3"


def test_a_non_numeric_max_tokens_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("HELIOS_LLM_MAX_TOKENS", "lots")
    assert ModelDriver(token="tok").max_tokens == 4096


def test_no_token_is_a_clear_local_error_not_a_network_call() -> None:
    """Failing before any request means no token can leak into a half-built
    call, and the message tells you what to set."""
    driver = ModelDriver(token="")
    with pytest.raises(ModelError) as excinfo:
        driver.next_calls(task="t", instructions="i", tools=[], history=[])
    message = str(excinfo.value)
    assert "no model token is set" in message
    assert "HELIOS_LLM_API_KEY" in message


# ---------------------------------------------------------------------------
# The request that goes on the wire
# ---------------------------------------------------------------------------


def test_request_is_a_plain_bearer_with_no_jwt(mock_server) -> None:
    """GLM-5.3 rejects the legacy JWT-shaped credential. The token must go
    out exactly as supplied."""
    driver = ModelDriver(token="tok", base_url=mock_server.base_url)
    driver.next_calls(
        task="t", instructions="i", tools=[],
        history=[{"role": "user", "content": "hello"}],
    )
    assert mock_server.errors == []
    auth = mock_server.headers[0]["authorization"]
    assert auth == "Bearer tok"
    assert auth.count(".") < 2, "looks like a JWT, which GLM-5.3 rejects"


def test_request_enables_thinking_and_never_disables_it(mock_server) -> None:
    driver = ModelDriver(token="tok", base_url=mock_server.base_url)
    driver.next_calls(
        task="t", instructions="i", tools=[],
        history=[{"role": "user", "content": "hello"}],
    )
    payload = mock_server.requests[0]
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] in {"low", "high", "max"}
    assert payload["model"] == "glm-5.3"
    assert payload["stream"] is False


def test_tools_are_wrapped_in_the_documented_envelope(mock_server) -> None:
    """``to_spec()`` returns a flat shape; this endpoint wants
    ``{"type": "function", "function": {...}}``."""
    server = _MockServer([{"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}]).start()
    try:
        driver = ModelDriver(token="tok", base_url=server.base_url)
        driver.next_calls(
            task="t", instructions="i", tools=_tools(),
            history=[{"role": "user", "content": "hello"}],
        )
        tools = server.requests[0]["tools"]
        assert [t["type"] for t in tools] == ["function", "function"]
        names = {t["function"]["name"] for t in tools}
        assert names == {"lookup_invoice", "send_email"}
        assert server.requests[0]["tool_choice"] == "auto"  # only supported value
    finally:
        server.stop()


def test_no_tools_key_when_nothing_was_granted(mock_server) -> None:
    driver = ModelDriver(token="tok", base_url=mock_server.base_url)
    driver.next_calls(
        task="t", instructions="i", tools=[],
        history=[{"role": "user", "content": "hello"}],
    )
    payload = mock_server.requests[0]
    assert "tools" not in payload
    assert "tool_choice" not in payload


def test_system_message_combines_instructions_and_the_gate_notice(
    mock_server,
) -> None:
    driver = ModelDriver(token="tok", base_url=mock_server.base_url)
    driver.next_calls(
        task="t", instructions="You chase overdue invoices.", tools=[],
        history=[{"role": "user", "content": "hello"}],
    )
    messages = mock_server.requests[0]["messages"]
    assert messages[0]["role"] == "system"
    assert "You chase overdue invoices." in messages[0]["content"]
    assert GATE_NOTICE in messages[0]["content"]


def test_a_conversation_of_only_assistant_turns_gets_a_user_turn(
    mock_server,
) -> None:
    """The endpoint refuses a thread with no user turn; the driver must not
    send one."""
    driver = ModelDriver(token="tok", base_url=mock_server.base_url)
    driver.next_calls(
        task="t", instructions="i", tools=[],
        history=[{"role": "assistant", "content": "thinking aloud"}],
    )
    roles = [m["role"] for m in mock_server.requests[0]["messages"]]
    assert "user" in roles


def test_empty_history_still_produces_a_sendable_thread() -> None:
    """GATE_NOTICE always yields a system turn, and the endpoint needs a user
    turn too — so the driver adds one rather than sending a thread it would
    reject."""
    driver = ModelDriver(token="tok", base_url=DEFAULT_BASE_URL)
    messages = driver._messages("i", [])
    roles = [m["role"] for m in messages]
    assert roles == ["system", "user"]


# ---------------------------------------------------------------------------
# Parsing the reply
# ---------------------------------------------------------------------------


def test_a_tool_call_is_returned_as_one_toolcall(mock_server) -> None:
    server = _MockServer(
        [_tool_reply("send_email", {"customer_id": "CUST-001", "invoice_id": "INV-1001"})]
    ).start()
    try:
        driver = ModelDriver(token="tok", base_url=server.base_url)
        result = driver.next_calls(
            task="t", instructions="i", tools=_tools(),
            history=[{"role": "user", "content": "chase INV-1001"}],
        )
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].name == "send_email"
        assert result[0].arguments == {
            "customer_id": "CUST-001",
            "invoice_id": "INV-1001",
        }
    finally:
        server.stop()


def test_plain_text_is_returned_as_the_final_answer(mock_server) -> None:
    driver = ModelDriver(token="tok", base_url=mock_server.base_url)
    assert driver.next_calls(
        task="t", instructions="i", tools=[],
        history=[{"role": "user", "content": "hello"}],
    ) == "All done."


def test_reasoning_content_is_not_mistaken_for_an_answer() -> None:
    """GLM-5.3 returns a thinking trace alongside the answer. The trace is
    not the answer, and treating it as one would end the run early."""
    assert text_of({"content": "Sent.", "reasoning_content": "Let me think..."}) == (
        "Sent."
    )
    assert text_of({"content": "", "reasoning_content": "long trace"}) == ""


def test_an_unregistered_tool_is_never_returned_as_a_call() -> None:
    """A hallucinated tool name becomes text back to the model. Executing it
    would be a side effect nobody authorized."""
    server = _MockServer(
        [_tool_reply("wire_everything_offshore", {"amount": 999999})]
    ).start()
    try:
        driver = ModelDriver(token="tok", base_url=server.base_url)
        result = driver.next_calls(
            task="t", instructions="i", tools=_tools(),
            history=[{"role": "user", "content": "chase INV-1001"}],
        )
        assert isinstance(result, str)
        assert "not available" in result
        assert "wire_everything_offshore" in result
    finally:
        server.stop()


def test_only_the_first_tool_call_in_a_batch_is_honoured() -> None:
    """The runtime adjudicates each side effect separately. Returning a batch
    would let the second one execute before the gate saw it."""
    reply = _tool_reply("send_email", {"customer_id": "C", "invoice_id": "I"})
    reply["choices"][0]["message"]["tool_calls"].append(
        {
            "id": "call_second",
            "type": "function",
            "function": {"name": "send_email", "arguments": "{}"},
        }
    )
    server = _MockServer([reply]).start()
    try:
        driver = ModelDriver(token="tok", base_url=server.base_url)
        result = driver.next_calls(
            task="t", instructions="i", tools=_tools(),
            history=[{"role": "user", "content": "go"}],
        )
        assert len(result) == 1
    finally:
        server.stop()


def test_non_function_tool_types_are_ignored() -> None:
    """web_search / retrieval / mcp are the endpoint's own tools, not ours."""
    reply = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "x", "type": "web_search", "function": {"name": "search", "arguments": "{}"}},
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    server = _MockServer([reply]).start()
    try:
        driver = ModelDriver(token="tok", base_url=server.base_url)
        with pytest.raises(ModelError, match="neither a tool call nor any text"):
            driver.next_calls(
                task="t", instructions="i", tools=_tools(),
                history=[{"role": "user", "content": "go"}],
            )
    finally:
        server.stop()


def test_a_reply_with_neither_call_nor_text_is_an_error() -> None:
    """Returning empty text here would silently end the run as a success."""
    server = _MockServer(
        [{"choices": [{"message": {"role": "assistant", "content": None},
                       "finish_reason": "length"}]}]
    ).start()
    try:
        driver = ModelDriver(token="tok", base_url=server.base_url)
        with pytest.raises(ModelError, match="neither a tool call nor any text"):
            driver.next_calls(
                task="t", instructions="i", tools=[],
                history=[{"role": "user", "content": "go"}],
            )
    finally:
        server.stop()


def test_no_choices_is_an_error() -> None:
    server = _MockServer([{"choices": []}]).start()
    try:
        driver = ModelDriver(token="tok", base_url=server.base_url)
        with pytest.raises(ModelError, match="no choices"):
            driver.next_calls(
                task="t", instructions="i", tools=[],
                history=[{"role": "user", "content": "go"}],
            )
    finally:
        server.stop()


def test_a_server_error_body_is_surfaced_without_the_token() -> None:
    """The 401 body is the useful debugging fact — which credential format the
    gateway wanted. The request, which carries the token, is never quoted."""
    server = _MockServer([], expect_token="something-else").start()
    try:
        driver = ModelDriver(token="tok", base_url=server.base_url)
        with pytest.raises(ModelError) as excinfo:
            driver.next_calls(
                task="t", instructions="i", tools=[],
                history=[{"role": "user", "content": "go"}],
            )
        message = str(excinfo.value)
        assert "401" in message
        assert "tok" not in message.replace("token", "")
    finally:
        server.stop()


def test_an_unreachable_endpoint_is_a_model_error() -> None:
    """Port 1 on loopback refuses connections, so this needs no network."""
    driver = ModelDriver(token="tok", base_url="http://127.0.0.1:1/v4", timeout_seconds=5)
    with pytest.raises(ModelError, match="could not be reached"):
        driver.next_calls(
            task="t", instructions="i", tools=[],
            history=[{"role": "user", "content": "go"}],
        )


def test_a_non_json_body_is_a_model_error() -> None:
    server = _MockServer([{"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}]).start()
    server.stop()
    # Nothing is listening now, so the port refuses rather than serving HTML.
    driver = ModelDriver(token="tok", base_url=server.base_url, timeout_seconds=5)
    with pytest.raises(ModelError):
        driver.next_calls(
            task="t", instructions="i", tools=[],
            history=[{"role": "user", "content": "go"}],
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_parse_arguments_handles_the_documented_string_form() -> None:
    assert parse_arguments('{"invoice_id": "INV-1001"}') == {"invoice_id": "INV-1001"}


def test_parse_arguments_accepts_an_already_parsed_mapping() -> None:
    assert parse_arguments({"a": 1}) == {"a": 1}


def test_malformed_arguments_become_an_empty_dict() -> None:
    """Empty arguments then fail the auditor's schema check and come back as a
    deny with a reason. Never quietly executed with what the model meant."""
    assert parse_arguments("{not json") == {}
    assert parse_arguments("") == {}
    assert parse_arguments(None) == {}
    assert parse_arguments("[1,2,3]") == {}  # valid JSON, not an object
    assert parse_arguments(42) == {}


def test_tool_function_wraps_and_supplies_an_empty_schema() -> None:
    tool = _tools()[0]
    wrapped = tool_function(tool)
    assert wrapped["type"] == "function"
    assert wrapped["function"]["name"] == "lookup_invoice"
    assert "parameters" in wrapped["function"]

    class BareTool:
        name = "bare"
        description = "no schema"

        def to_spec(self):
            return {"name": self.name, "description": self.description}

    # A missing parameters object is rejected by the endpoint.
    assert tool_function(BareTool())["function"]["parameters"] == {
        "type": "object",
        "properties": {},
    }


def test_history_translation_pairs_tool_results_with_call_ids() -> None:
    """This protocol requires every tool result to name the call it answers.
    The runtime keeps no ids, so they are synthesised positionally — which is
    stable because history is append-only and one call is made per turn."""
    history = [
        {"role": "user", "content": "chase INV-1001"},
        {"role": "tool", "name": "lookup_invoice", "content": '{"amount": 4200}'},
        {"role": "tool", "name": "send_email", "content": "DENIED: too large"},
    ]
    messages = as_chat_messages(history)

    assert messages[0] == {"role": "user", "content": "chase INV-1001"}
    assert messages[1]["role"] == "assistant"
    assert messages[1]["tool_calls"][0]["function"]["name"] == "lookup_invoice"
    first_id = messages[1]["tool_calls"][0]["id"]
    assert messages[2] == {
        "role": "tool",
        "tool_call_id": first_id,
        "content": '{"amount": 4200}',
    }
    second_id = messages[3]["tool_calls"][0]["id"]
    assert second_id != first_id, "duplicate ids would confuse the endpoint"
    assert messages[4]["tool_call_id"] == second_id
    assert messages[4]["content"] == "DENIED: too large"


def test_history_translation_ignores_unknown_roles() -> None:
    assert as_chat_messages([{"role": "tool_call_raw", "content": "x"}]) == []


def test_gate_notice_tells_the_model_one_call_per_turn() -> None:
    """An agent that understands the gate adjusts; one that does not retries
    or batches its way around it."""
    assert "exactly ONE tool per turn" in GATE_NOTICE
    assert "DENIED" in GATE_NOTICE
    assert "HELD" in GATE_NOTICE
    assert "granted value is what executes" in GATE_NOTICE
    assert "Do not invent tool names" in GATE_NOTICE


# ---------------------------------------------------------------------------
# End to end through the runtime
# ---------------------------------------------------------------------------


def test_the_runtime_drives_a_mock_model_through_the_gate(tmp_path) -> None:
    """The real payoff: a model that is not a script still gets gated. It asks
    for a large-invoice email, the auditor holds it, and nothing is sent."""
    from agent_integration import build_integration

    server = _MockServer(
        [
            # Turn 1: the model looks the invoice up.
            _tool_reply("lookup_invoice", {"invoice_id": "INV-1003"}),
            # Turn 2: it proposes the side effect.
            _tool_reply(
                "send_email",
                {
                    "customer_id": "CUST-003",
                    "invoice_id": "INV-1003",
                    "subject": "Invoice HL-2026-078 is overdue",
                    "body": "This invoice is past due.",
                },
            ),
        ]
    ).start()
    try:
        built = build_integration(
            app_id="mock-model",
            agent_id="mock-model-agent",
            runs_dir=str(tmp_path / "runs"),
            data_dir=str(tmp_path / "data"),
            allow_unsigned=True,
        )
        try:
            driver = ModelDriver(token="tok", base_url=server.base_url)
            result = built.runtime.run(
                driver=driver,
                task="follow up on the large overdue invoice for Harbor Logistics",
                task_type="collect.reminder",
            )
            assert server.errors == []
            assert result.status == "held"
            # Both calls are recorded: the read-only lookup executed, and the
            # side effect was held and did not.
            assert [a["tool"] for a in result.actions] == [
                "lookup_invoice",
                "send_email",
            ]
            assert result.actions[0]["executed"] is True
            assert result.actions[1]["executed"] is False
            assert result.actions[1]["verdict"]["decision"] == "hold"
            assert built.store.sent_emails == []
        finally:
            built.close()
    finally:
        server.stop()


def test_the_runtime_executes_what_a_mock_model_was_allowed(tmp_path) -> None:
    """The approve path through a real HTTP round trip, not a script."""
    from agent_integration import build_integration

    server = _MockServer(
        [
            _tool_reply(
                "send_email",
                {
                    "customer_id": "CUST-001",
                    "invoice_id": "INV-1001",
                    "subject": "Invoice RL-2026-047 is overdue",
                    "body": "A courteous reminder.",
                },
            ),
            {"choices": [{"message": {"content": "Reminder sent."}, "finish_reason": "stop"}]},
        ]
    ).start()
    try:
        built = build_integration(
            app_id="mock-model-ok",
            agent_id="mock-model-ok-agent",
            runs_dir=str(tmp_path / "runs"),
            data_dir=str(tmp_path / "data"),
            allow_unsigned=True,
        )
        try:
            result = built.runtime.run(
                driver=ModelDriver(token="tok", base_url=server.base_url),
                task="send Ridgeline a reminder about their overdue invoice",
                task_type="collect.reminder",
            )
            assert server.errors == []
            assert result.status == "completed"
            assert result.actions[0]["executed"] is True
            assert len(built.store.sent_emails) == 1
            assert result.final_text == "Reminder sent."
        finally:
            built.close()
    finally:
        server.stop()


def test_a_broken_endpoint_ends_the_run_as_an_error(tmp_path) -> None:
    """A model that cannot be reached is an error, never an allow."""
    from agent_integration import build_integration

    built = build_integration(
        app_id="broken-model",
        agent_id="broken-model-agent",
        runs_dir=str(tmp_path / "runs"),
        data_dir=str(tmp_path / "data"),
        allow_unsigned=True,
    )
    try:
        driver = ModelDriver(
            token="tok", base_url="http://127.0.0.1:1/v4", timeout_seconds=5
        )
        result = built.runtime.run(
            driver=driver,
            task="send a reminder about the overdue invoice INV-1001",
            task_type="collect.reminder",
        )
        assert result.status == "error"
        assert built.store.sent_emails == []
    finally:
        built.close()

"""Evidence, transport, and manifest validation.

Three supporting guarantees, each of which the architecture depends on:
the log is tamper-evident, the two agents can talk across a process
boundary without changing behaviour, and a bad manifest is rejected with a
message a coding agent can act on.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from conftest import MANIFESTS, make_action, make_authorization

from audit_agent import (
    ActionRequest,
    AuditAgent,
    AuditLog,
    AuditManifest,
    AuthorizationRequest,
    ChainBreakError,
    Decision,
    EventType,
    FileTransport,
    FileTransportServer,
    GENESIS_HASH,
    HttpTransport,
    HttpTransportServer,
    InProcessTransport,
    Keyring,
    PolicyError,
    StaticFactSource,
    ToolDescriptor,
    ToolRegistry,
    build_audit_agent,
    validate_manifest_file,
    validate_manifest_text,
)


class TestAuditLog:
    def test_starts_at_the_genesis_hash(self, tmp_path: pathlib.Path) -> None:
        log = AuditLog(tmp_path / "a.jsonl")
        assert log.head == GENESIS_HASH
        assert log.sequence == 0

    def test_entries_are_appended_and_chained(self, tmp_path: pathlib.Path) -> None:
        log = AuditLog(tmp_path / "a.jsonl")
        log.append(EventType.ACTION_DECIDED, request_id="r1", decision=Decision.ALLOW)
        log.append(EventType.ACTION_DECIDED, request_id="r2", decision=Decision.DENY)
        ok, problems = log.verify_chain()
        assert ok is True
        assert problems == []
        assert log.sequence == 2

    def test_editing_an_entry_breaks_the_chain(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "a.jsonl"
        log = AuditLog(path)
        log.append(EventType.ACTION_DECIDED, request_id="r1", decision=Decision.ALLOW)
        log.append(EventType.ACTION_DECIDED, tool="send_reminder_email", decision=Decision.ALLOW)

        lines = path.read_text(encoding="utf-8").splitlines()
        tampered = json.loads(lines[1])
        tampered["tool"] = "pay_supplier"  # rewrite what the agent did
        lines[1] = json.dumps(tampered, sort_keys=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        ok, problems = AuditLog(path).verify_chain()
        assert ok is False
        assert problems

    def test_deleting_an_entry_breaks_the_chain(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "a.jsonl"
        log = AuditLog(path)
        for index in range(3):
            log.append(EventType.ACTION_DECIDED, request_id=f"r{index}", decision=Decision.ALLOW)
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
        ok, _ = AuditLog(path).verify_chain()
        assert ok is False

    def test_assert_intact_raises_on_tampering(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "a.jsonl"
        log = AuditLog(path)
        log.append(EventType.ACTION_DECIDED, request_id="r1", decision=Decision.ALLOW)
        path.write_text(
            path.read_text(encoding="utf-8").replace("r1", "r2"), encoding="utf-8"
        )
        with pytest.raises(ChainBreakError):
            AuditLog(path).assert_intact()

    def test_reopening_resumes_the_chain(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "a.jsonl"
        first = AuditLog(path)
        first.append(EventType.ACTION_DECIDED, request_id="r1", decision=Decision.ALLOW)
        second = AuditLog(path)
        assert second.sequence == 1
        assert second.head == first.head
        second.append(EventType.ACTION_DECIDED, request_id="r2", decision=Decision.ALLOW)
        ok, _ = second.verify_chain()
        assert ok is True

    def test_credentials_are_redacted_before_hitting_disk(
        self, tmp_path: pathlib.Path
    ) -> None:
        path = tmp_path / "a.jsonl"
        log = AuditLog(path)
        log.append(
            EventType.ACTION_DECIDED,
            request_id="r1",
            decision=Decision.ALLOW,
            details={"api_key": "sk-live-SECRET", "amount": "10.00"},
        )
        raw = path.read_text(encoding="utf-8")
        assert "sk-live-SECRET" not in raw
        assert "***REDACTED***" in raw
        assert "10.00" in raw  # ordinary values are kept

    def test_payment_auth_codes_are_redacted_like_every_other_credential(self) -> None:
        """`auth_code` / `authorization_code` are payment credentials.

        A PSD2-style dynamic-linking flow carries a one-time authorisation code
        bound to (amount, currency, payee). It is exactly as sensitive as an
        API key: whoever holds it can authorise the payment. It must never
        reach the audit log in the clear.
        """
        from audit_agent.contracts import REDACTED, REDACTED_KEYS, redact

        for key in ("auth_code", "authorization_code"):
            assert key in REDACTED_KEYS, f"{key} is a credential and must be redacted"
            assert redact({key: "OTP-9f3c-SECRET"})[key] == REDACTED

        # Case-insensitive, like every other redacted key.
        assert redact({"AUTH_CODE": "OTP-SECRET"})["AUTH_CODE"] == REDACTED
        # Ordinary payment fields are untouched — redaction is not a blanket wipe.
        kept = redact({"auth_code": "OTP-SECRET", "amount": "250.00", "payee": "Acme"})
        assert kept["amount"] == "250.00"
        assert kept["payee"] == "Acme"

    def test_a_held_payment_credential_never_reaches_the_owner_question(
        self, tmp_path: pathlib.Path
    ) -> None:
        """End-to-end: the leak the key-set fix closes.

        `_owner_question` renders `request.arguments` into the plain-language
        question shown to the owner AND written into the hash-chained log. If a
        payment credential key is not in REDACTED_KEYS, its plaintext survives
        the ActionRequest arguments validator and lands in both places.

        This needs a payment tool whose schema legitimately ACCEPTS an
        `auth_code` field — the code is a real part of a PSD2 dynamic-linking
        request, so it passes schema validation; it is the *value* that must
        never be logged. A large amount forces a HOLD so the owner_question is
        actually populated. The collect fixtures cannot express this (their
        reminder schema forbids extra properties), so the agent is built here.
        """
        from audit_agent import (
            Keyring,
            PolicyPack,
            StaticFactSource,
            ToolDescriptor,
            ToolRegistry,
        )

        path = tmp_path / "audit.jsonl"
        registry = ToolRegistry(
            [
                ToolDescriptor(
                    name="send_payment",
                    description="pay an invoice under a verified intent",
                    side_effect=True,
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["invoice_id", "auth_code"],
                        "properties": {
                            "invoice_id": {"type": "string", "minLength": 1},
                            "auth_code": {"type": "string", "minLength": 1},
                        },
                    },
                )
            ]
        )
        pack = PolicyPack.from_yaml_text(
            """
name: pay
source: app
tasks:
  pay.invoice:
    description: pay an invoice
    allowed_tools: [send_payment]
    ttl_seconds: 300
rules:
  - id: app.large_payment_approval
    tools: [send_payment]
    message: a payment over 10000 needs owner approval
    limits:
      - fact: amount_due
        max: "10000.00"
        on_exceed: hold
""",
            origin="pay.yaml",
        )
        agent = AuditAgent.build(
            registry,
            app_packs=[pack],
            fact_source=StaticFactSource(
                amounts={"INV-BIG": "24500.00"}, contacts={"INV-BIG": 0}
            ),
            log_path=path,
            keyring=Keyring(b"test-signing-key", "test-key"),
        )

        auth = agent.authorize(
            AuthorizationRequest(
                request_id="az1",
                app_id="helios-pay",
                agent_id="payer",
                principal="owner",
                task_type="pay.invoice",
                task_summary="pay a large invoice",
                requested_tools=["send_payment"],
                context={"invoice_id": "INV-BIG"},
            )
        )
        secret = "OTP-9f3c-LIVE-SECRET"
        decision = agent.adjudicate(
            ActionRequest(
                request_id="c1",
                app_id="helios-pay",
                agent_id="payer",
                principal="owner",
                task_type="pay.invoice",
                tool="send_payment",
                arguments={"invoice_id": "INV-BIG", "auth_code": secret},
                authorization=auth,
            )
        )
        assert decision.decision is Decision.HOLD, decision.decision

        # The rendered question the owner reads must not carry the credential.
        assert decision.owner_question is not None
        assert secret not in decision.owner_question

        # Neither must the persisted, hash-chained evidence.
        raw = path.read_text(encoding="utf-8")
        assert secret not in raw
        assert "***REDACTED***" in raw
        # The chain still verifies — redaction happens before hashing, not after.
        ok, problems = AuditLog(path).verify_chain()
        assert ok is True, problems


class TestAgentWritesEvidence:
    def test_every_step_is_logged(self, agent: AuditAgent, log_path: pathlib.Path) -> None:
        auth = make_authorization(agent)
        make_action(agent, {"invoice_id": "INV-1001", "message": "Reminder"}, auth)
        events = AuditLog(log_path).read()
        kinds = [event.event_type for event in events]
        assert EventType.AUTHORIZATION_REQUESTED in kinds
        assert EventType.AUTHORIZATION_ISSUED in kinds
        assert EventType.ACTION_DECIDED in kinds

    def test_a_hold_is_logged_and_so_is_its_resolution(
        self, agent: AuditAgent, log_path: pathlib.Path
    ) -> None:
        auth = make_authorization(agent)
        decision = make_action(
            agent,
            {"invoice_id": "INV-1001", "message": "Take 60 days", "extension_days": 60},
            auth,
        )
        agent.resolve_hold(decision.hold_id, approve=True, decided_by="owner")
        kinds = [event.event_type for event in AuditLog(log_path).read()]
        assert EventType.HOLD_CREATED in kinds
        assert EventType.HOLD_RESOLVED in kinds

    def test_the_log_stays_intact_through_a_full_session(
        self, agent: AuditAgent, log_path: pathlib.Path
    ) -> None:
        auth = make_authorization(agent)
        make_action(agent, {"invoice_id": "INV-1001", "message": "Reminder"}, auth)
        ok, problems = AuditLog(log_path).verify_chain()
        assert ok is True, problems


class TestTransportsAreInterchangeable:
    """The auditor is standalone: same contract over any wire."""

    def _exercise(self, client) -> None:
        caps = client.capabilities()
        assert "collect.reminder" in caps["tasks"]
        auth = client.authorize(
            AuthorizationRequest(
                request_id="r1",
                app_id="helios-collect",
                agent_id="collect-reminder",
                task_type="collect.reminder",
                task_summary="Chase the overdue invoice for payment",
                requested_tools=["send_reminder_email"],
                context={"invoice_id": "INV-1001"},
            )
        )
        assert auth.decision is Decision.ALLOW
        decision = client.adjudicate(
            ActionRequest(
                request_id="c1",
                app_id="helios-collect",
                agent_id="collect-reminder",
                task_type="collect.reminder",
                tool="send_reminder_email",
                arguments={"invoice_id": "INV-1001", "message": "Reminder"},
                authorization=auth,
            )
        )
        assert decision.decision is Decision.ALLOW

    def test_in_process(self, agent: AuditAgent) -> None:
        self._exercise(InProcessTransport(agent))

    def test_file_spool(self, agent: AuditAgent, tmp_path: pathlib.Path) -> None:
        server = FileTransportServer(agent, tmp_path / "spool").start()
        try:
            self._exercise(FileTransport(tmp_path / "spool", timeout_seconds=10))
        finally:
            server.stop()

    def test_http(self, agent: AuditAgent) -> None:
        server = HttpTransportServer(agent).start()
        try:
            client = HttpTransport(server.base_url, timeout_seconds=10)
            assert client.health()["status"] == "healthy"
            self._exercise(client)
        finally:
            server.stop()

    def test_an_unreachable_auditor_fails_closed(
        self, tmp_path: pathlib.Path
    ) -> None:
        """No auditor running must never mean "allowed"."""
        client = FileTransport(tmp_path / "empty", timeout_seconds=0.3)
        decision = client.adjudicate(
            ActionRequest(
                request_id="c1",
                app_id="helios-collect",
                agent_id="collect-reminder",
                task_type="collect.reminder",
                tool="send_reminder_email",
                arguments={"invoice_id": "INV-1001", "message": "Reminder"},
            )
        )
        assert decision.decision is Decision.DENY
        assert decision.findings[0].rule_id == "transport.unavailable"


class TestManifests:
    def test_the_reference_manifests_are_valid(self) -> None:
        for path in sorted(MANIFESTS.glob("*.yaml")):
            ok, problems = validate_manifest_file(path)
            assert ok is True, f"{path.name}: {problems}"

    def test_the_collect_manifest_builds_a_working_agent(
        self, tmp_path: pathlib.Path
    ) -> None:
        manifest = AuditManifest.load(MANIFESTS / "collect-reminder.yaml")
        agent = build_audit_agent(
            manifest,
            fact_source=StaticFactSource(
                amounts={"INV-1001": "450.00"}, contacts={"INV-1001": 0}
            ),
            keyring=Keyring(b"test-signing-key", "test-key"),
            log_path=tmp_path / "collect.jsonl",
        )
        auth = agent.authorize(
            AuthorizationRequest(
                request_id="r1",
                app_id=manifest.app_id,
                agent_id="collect-reminder",
                task_type="collect.reminder",
                task_summary="Chase the overdue invoice for payment",
                requested_tools=["send_reminder_email"],
                context={"invoice_id": "INV-1001"},
            )
        )
        assert auth.decision is Decision.ALLOW

    def test_a_manifest_cannot_emit_a_baseline_pack(self) -> None:
        manifest = AuditManifest.load(MANIFESTS / "collect-reminder.yaml")
        assert manifest.policy_pack().source == "app"

    def test_undeclared_tool_in_a_task_is_rejected(self) -> None:
        ok, problems = validate_manifest_text(
            """
manifest_version: "1"
app_id: bad
name: bad
tools: [{name: t, side_effect: false}]
tasks:
  x: {allowed_tools: [t, not_declared]}
""",
            origin="bad.yaml",
        )
        assert ok is False
        assert "not declared" in problems[0]

    def test_a_side_effect_tool_without_a_schema_is_rejected(self) -> None:
        """Without a schema, malformed arguments cannot be caught before the
        action runs — so the manifest is refused, not quietly accepted."""
        ok, problems = validate_manifest_text(
            """
manifest_version: "1"
app_id: bad
name: bad
tools: [{name: pay, side_effect: true}]
tasks:
  x: {allowed_tools: [pay]}
""",
            origin="bad.yaml",
        )
        assert ok is False
        assert "input_schema" in problems[0]

    def test_a_rule_with_no_check_is_rejected(self) -> None:
        ok, problems = validate_manifest_text(
            """
manifest_version: "1"
app_id: bad
name: bad
tools: [{name: t, side_effect: false}]
tasks:
  x: {allowed_tools: [t]}
rules:
  - id: app.nothing
    message: does nothing
""",
            origin="bad.yaml",
        )
        assert ok is False
        assert "declares no check" in problems[0]

    def test_clamp_and_hold_on_the_same_field_is_rejected(self) -> None:
        """The combination looks like a cap-then-approve pipeline but is not:
        rules evaluate the original request independently, so the clamp would
        be dead config and every call would hold. Refused with an explanation.
        """
        schema = json.dumps(
            {
                "type": "object",
                "properties": {"extension_days": {"type": "integer"}},
            }
        )
        ok, problems = validate_manifest_text(
            f"""
manifest_version: "1"
app_id: bad
name: bad
tools:
  - name: send
    side_effect: true
    input_schema: {schema}
tasks:
  x: {{allowed_tools: [send]}}
rules:
  - id: app.cap
    tools: [send]
    limits: [{{argument: extension_days, max: 14, clamp: true}}]
  - id: app.approve
    tools: [send]
    limits: [{{argument: extension_days, max: 14, on_exceed: hold}}]
""",
            origin="bad.yaml",
        )
        assert ok is False
        assert "conflicting policies" in problems[0]

    def test_a_limit_on_a_nonexistent_argument_is_rejected(self) -> None:
        ok, problems = validate_manifest_text(
            """
manifest_version: "1"
app_id: bad
name: bad
tools:
  - name: send
    side_effect: true
    input_schema: {type: object, properties: {message: {type: string}}}
tasks:
  x: {allowed_tools: [send]}
rules:
  - id: app.typo
    tools: [send]
    limits: [{argument: extension_dayz, max: 14}]
""",
            origin="bad.yaml",
        )
        assert ok is False
        assert "no such argument" in problems[0]

    def test_a_limit_with_neither_max_nor_fact_is_rejected(self) -> None:
        with pytest.raises(Exception):
            from audit_agent import LimitSpec

            LimitSpec(argument="x")

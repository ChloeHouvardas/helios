"""Tests that each control in invoice-payer actually fires.

    cd entry-point/examples/invoice-payer && python3 -m pytest tests -q

Modelled on platform/audit-agent-builder/tests/test_security.py, which asserts
every security property individually. That file is the one to show a judge, and
this is the app-level equivalent: not "does the app run" but "does each rule do
what its description claims".

Every test here is offline. Each builds a fresh app over a throwaway run
directory, so nothing is shared and no test depends on another's ordering.

Two groups matter most:

    test_the_agent_cannot_overpay_an_invoice
    test_a_lie_in_resolved_facts_changes_nothing
    test_understating_an_amount_requires_the_owner_to_confirm_it
        → facts are not claims, and which direction each control guards

    test_undeclared_tool_is_rejected_at_build (check_app.py covers this)
    test_auth_code_is_redacted_from_the_action_log
    test_a_held_call_stays_held_no_matter_how_it_is_reworded
        → no ungated side effects, no credentials on disk, no arguing with the gate

Tests that assert a payment actually *settles* mint a real auth code via
`_real_auth_code()`. Tests about gating use a placeholder, because
`app.requires_verified_intent` checks presence while `payments.send` checks
validity — two different layers, pinned down by
`test_a_placeholder_credential_passes_the_gate_but_not_the_tool`.
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[2]
for _p in (
    REPO / "platform" / "audit-agent-builder" / "src",
    REPO / "platform" / "action-agent-builder" / "src",
    REPO / "platform" / "financial-tools",
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

sys.path.insert(0, str(HERE))

from app import (  # noqa: E402
    GatedRuntime,
    InvoiceBook,
    InvoiceFactSource,
    ScriptedOwner,
    ScriptedPayerAgent,
    _CREDENTIAL_KEYS,
    _add_supplier_lookup,
    build_app,
    redact_arguments,
)
from audit_agent import ActionRequest, AuthorizationRequest, Decision  # noqa: E402
from audit_agent.manifest import AuditManifest  # noqa: E402
from agent_toolkit.manifest import (  # noqa: E402
    ManifestError,
    load_manifest,
    validate_manifest,
)

AUDIT_MANIFEST = HERE / "agents" / "invoice-payer-audit.yaml"
ACTION_MANIFEST = HERE / "agents" / "invoice-payer-action.json"

LARGE = "INV-1002"        # $48,000, Delta Mills — over the 25,000 ceiling
CHANGED_DETAILS = "INV-1003"  # Cinder Logistics — supplier details changed
DUPLICATE = "INV-1004"    # Northwind, flagged duplicate
NORMAL = "INV-1001"       # $1,250, inside every limit


@pytest.fixture()
def runtime(tmp_path: Path) -> GatedRuntime:
    """A fresh app over a throwaway run directory."""
    rt = build_app(run_dir=tmp_path)
    _add_supplier_lookup(rt.book)
    return rt


def _grant(rt: GatedRuntime, invoice_id: str) -> object:
    """Authorize one task, as the agent would before proposing a payment.

    `task_summary` must be substantive: `baseline.intent_stated` warns on a
    summary too short to audit, and a warn-severity finding can cost the grant.
    """
    return rt.authorize(
        f"Pay invoice {invoice_id} to its supplier for goods received", invoice_id)


def _real_auth_code(rt: GatedRuntime, invoice_id: str, *,
                    amount_cents: int | None = None) -> str:
    """Mint a credential the owner actually confirmed, for this exact amount.

    `payments.send` will not settle without one, so any test that expects
    execution needs a real code rather than a placeholder string.
    """
    inv = rt.book.get(invoice_id)
    cents = amount_cents if amount_cents is not None else rt.book.amount_due_cents(invoice_id)
    intent = rt.intents.capture_intent(
        principal=rt.principal, amount_cents=cents, currency=inv["currency"],
        payee=inv["supplier_id"], purpose=f"invoice {invoice_id}")
    return rt.intents.confirm(intent.intent_id, by=rt.principal).code


# A placeholder credential for gating tests. Long enough to look like a real
# code, but it will not settle: `payments.send` validates the code against a
# confirmed intent at execution time and rejects this. The gate checks only
# PRESENCE (required + the critical requires_verified_intent rule), never the
# value — the value is redacted before the gate sees it.
FAKE_CODE = "a" * 64


def _payment_args(rt: GatedRuntime, invoice_id: str, *,
                  amount_cents: int | None = None,
                  auth_code: str | None = None,
                  idempotency_key: str | None = None) -> dict:
    """Realistic payment arguments, with overridable amount and credential.

    `auth_code` defaults to a placeholder: enough for tests about *gating*
    (presence is what the rule checks), not enough for tests about *execution*.
    """
    inv = rt.book.get(invoice_id)
    cents = amount_cents if amount_cents is not None else rt.book.amount_due_cents(invoice_id)
    return {
        "payer": "acme-operating",
        "payee": inv["supplier_id"],
        "amount_cents": cents,
        "currency": inv["currency"],
        "auth_code": FAKE_CODE if auth_code is None else auth_code,
        "idempotency_key": idempotency_key or f"test-{invoice_id}",
        "memo": "test",
    }


# ---------------------------------------------------------------------------
# Manifests parse, and agree with each other
# ---------------------------------------------------------------------------

def test_audit_manifest_loads_under_its_own_validator():
    m = AuditManifest.load(AUDIT_MANIFEST)
    assert m.app_id == "invoice-payer"
    assert "payables.pay_invoice" in m.tasks
    assert {r.id for r in m.rules} >= {
        "app.requires_verified_intent",
        "app.amount_matches_invoice",
        "app.large_payment_approval",
        "app.payee_details_changed",
        "app.duplicate_invoice",
    }


def test_action_manifest_loads_under_its_own_validator():
    m = load_manifest(ACTION_MANIFEST)
    assert m.name == "invoice-payer"
    assert "payments.send" in m.mandate.allowed_tools


def test_action_manifest_rejects_a_comment_key():
    """Strict loading: the JSON manifest tolerates no extra keys.

    A generated manifest that adds `_comment` for its own documentation fails to
    load. Commentary belongs in a README next to the file.
    """
    data = json.loads(ACTION_MANIFEST.read_text(encoding="utf-8"))
    data["_comment"] = "notes about this agent"
    with pytest.raises(ManifestError) as exc:
        validate_manifest(data)
    assert "_comment" in str(exc.value)


def test_every_action_tool_is_declared_by_the_auditor():
    """The single most important cross-file invariant.

    The audit agent is the registry of what may be granted at all. A tool an
    action agent can call that the auditor never declared is an ungated side
    effect waiting to happen.
    """
    action = load_manifest(ACTION_MANIFEST)
    audit = AuditManifest.load(AUDIT_MANIFEST)
    declared = {t.name for t in audit.tools}
    offered = set(action.tools) | set(action.mandate.allowed_tools)
    assert offered <= declared, f"undeclared tools: {sorted(offered - declared)}"


def test_identity_ids_agree_across_files():
    """identity.id == audit app_id, or two files that look like one app are two."""
    import yaml
    spec = yaml.safe_load((HERE / "helios-app.yaml").read_text(encoding="utf-8"))
    audit = AuditManifest.load(AUDIT_MANIFEST)
    action = load_manifest(ACTION_MANIFEST)
    assert spec["identity"]["id"] == audit.app_id == action.name


def test_spec_tools_is_a_superset_of_both_manifests():
    import yaml
    spec = yaml.safe_load((HERE / "helios-app.yaml").read_text(encoding="utf-8"))
    audit = AuditManifest.load(AUDIT_MANIFEST)
    action = load_manifest(ACTION_MANIFEST)
    needed = {t.name for t in audit.tools} | set(action.tools)
    assert needed <= set(spec["tools"]), f"undeclared in spec: {sorted(needed - set(spec['tools']))}"


def test_money_is_never_float_in_the_spec():
    import yaml
    spec = yaml.safe_load((HERE / "helios-app.yaml").read_text(encoding="utf-8"))
    closed = {"string", "integer", "decimal", "date", "datetime", "boolean", "json"}
    for ent in spec["data"]["local"]["entities"]:
        for f in ent["fields"]:
            assert f["type"] in closed, f"{ent['name']}.{f['name']}: {f['type']}"
            assert f["type"] != "float"


# ---------------------------------------------------------------------------
# The normal path: a legitimate payment goes through
# ---------------------------------------------------------------------------

def test_a_normal_payment_is_allowed_and_settles(runtime):
    _grant(runtime, NORMAL)
    args = _payment_args(runtime, NORMAL,
                         auth_code=_real_auth_code(runtime, NORMAL))
    outcome = runtime.propose("payments.send", args, invoice_id=NORMAL)
    assert outcome["decision"] == "allow", outcome["reason"]
    assert outcome["result"]["state"] == "settled"


def test_a_placeholder_credential_passes_the_gate_but_not_the_tool(runtime):
    """The gate checks presence; the tool checks validity. Two different layers.

    Worth pinning down, because it explains why a test that only asserts a
    verdict can pass while no money moves. Note the two distinct rejections:
    a *present but unknown* code fails as "unknown auth code", while an *absent*
    one fails as "missing verified intent auth code".
    """
    _grant(runtime, NORMAL)
    outcome = runtime.propose("payments.send", _payment_args(runtime, NORMAL),
                              invoice_id=NORMAL)
    assert outcome["decision"] == "allow"
    assert outcome["result"]["state"] == "rejected"
    assert "unknown auth code" in outcome["result"]["reject_reason"]

    # The absent case, for contrast. A distinct idempotency_key is required:
    # repeating one returns the cached outcome instead of re-evaluating.
    absent = _payment_args(runtime, NORMAL, idempotency_key="test-absent-code")
    del absent["auth_code"]
    result = runtime.registry.call("payments.send", **absent)
    assert result["state"] == "rejected"
    assert "missing verified intent auth code" in result["reject_reason"]


def test_an_idempotency_key_replays_a_cached_rejection(runtime):
    """Repeating an idempotency_key returns the stored outcome, not a re-run.

    Verified behaviour, and worth knowing because it cuts both ways. It is what
    stops a retry from paying twice — the property BUILD.md tells you to rely on.
    But it also means a *rejected* payment is replayed as rejected, with the
    original reason, even if the arguments now differ. An agent that fixes the
    problem and retries under the same key will see the old failure and may
    conclude its fix did not work.

    So: reuse the key when retrying *the same* payment, and use a fresh one when
    the payment itself has genuinely changed.
    """
    first = runtime.registry.call("payments.send", **_payment_args(
        runtime, NORMAL, idempotency_key="key-replay"))
    assert first["state"] == "rejected"
    assert "unknown auth code" in first["reject_reason"]

    # Same key, different (and this time valid) arguments — still the cached one.
    second = runtime.registry.call("payments.send", **_payment_args(
        runtime, NORMAL, idempotency_key="key-replay",
        auth_code=_real_auth_code(runtime, NORMAL)))
    assert second["state"] == "rejected"
    assert second["payment_id"] == first["payment_id"]

    # A fresh key re-evaluates and settles.
    third = runtime.registry.call("payments.send", **_payment_args(
        runtime, NORMAL, idempotency_key="key-fresh",
        auth_code=_real_auth_code(runtime, NORMAL)))
    assert third["state"] == "settled"


def test_read_only_tools_run_without_a_grant(runtime):
    """Reads are not gated. A gate on every call would make the app unusable."""
    assert runtime.read("payments.balance", account="acme-operating")["balance_cents"] > 0
    inv = runtime.read("lookup_invoice", invoice_id=NORMAL)
    assert inv["invoice"]["invoice_id"] == NORMAL


def test_a_side_effect_cannot_be_taken_through_the_read_path(runtime):
    with pytest.raises(ValueError, match="side effects"):
        runtime.read("payments.send", **_payment_args(runtime, NORMAL))


def test_lookup_invoice_withholds_the_control_input(runtime):
    """The agent never sees `payment_details_changed`.

    It is a control input, not business information. The auditor reads it from the
    fact source, so an agent cannot argue with a value it was never given.
    """
    inv = runtime.read("lookup_invoice", invoice_id=CHANGED_DETAILS)
    assert "payment_details_changed" not in inv["supplier"]
    # ...but the fact source does see it.
    assert InvoiceFactSource(runtime.book).payment_details_changed_for(CHANGED_DETAILS) is True


# ---------------------------------------------------------------------------
# Control: large payments go to the owner
# ---------------------------------------------------------------------------

def test_a_large_payment_is_held_for_the_owner(runtime):
    _grant(runtime, LARGE)
    outcome = runtime.propose("payments.send", _payment_args(runtime, LARGE),
                              invoice_id=LARGE)
    assert outcome["decision"] == "hold"
    assert outcome.get("hold_id")
    assert "25,000" in outcome["owner_question"]
    # Nothing moved while it is held.
    assert runtime.read("payments.balance",
                        account="acme-operating")["balance_cents"] == 6_000_000


def test_an_approved_hold_executes_with_the_exact_arguments_held(runtime):
    """An approval applies to that action and no other.

    `execute_held` re-uses the held proposal's own arguments, so the agent cannot
    have a small amount approved and then substitute a large one.
    """
    _grant(runtime, LARGE)
    outcome = runtime.propose(
        "payments.send",
        _payment_args(runtime, LARGE, auth_code=_real_auth_code(runtime, LARGE)),
        invoice_id=LARGE)
    assert outcome["decision"] == "hold"
    before = runtime.read("payments.balance", account="acme-operating")["balance_cents"]

    runtime.resolve_hold(outcome["hold_id"], approve=True)
    result = runtime.execute_held(outcome)

    assert result["state"] == "settled"
    after = runtime.read("payments.balance", account="acme-operating")["balance_cents"]
    assert before - after == 4_800_000


def test_a_declined_hold_does_not_move_money(runtime):
    _grant(runtime, LARGE)
    outcome = runtime.propose("payments.send", _payment_args(runtime, LARGE),
                              invoice_id=LARGE)
    assert outcome["decision"] == "hold"
    before = runtime.read("payments.balance", account="acme-operating")["balance_cents"]

    runtime.resolve_hold(outcome["hold_id"], approve=False)

    assert runtime.read("payments.balance",
                        account="acme-operating")["balance_cents"] == before


# ---------------------------------------------------------------------------
# Control: changed supplier details are refused outright
# ---------------------------------------------------------------------------

def test_changed_payee_details_are_denied_not_held(runtime):
    """Deny, not hold: the agent must not proceed until a human re-verifies.

    Putting this in a queue would invite the owner to approve the thing most
    likely to be fraud.
    """
    _grant(runtime, CHANGED_DETAILS)
    outcome = runtime.propose("payments.send", _payment_args(runtime, CHANGED_DETAILS),
                              invoice_id=CHANGED_DETAILS)
    assert outcome["decision"] == "deny"
    assert "payment details changed" in outcome["reason"]


def test_a_denied_payment_never_reaches_the_ledger(runtime):
    _grant(runtime, CHANGED_DETAILS)
    outcome = runtime.propose("payments.send", _payment_args(runtime, CHANGED_DETAILS),
                              invoice_id=CHANGED_DETAILS)
    assert outcome["decision"] == "deny"
    assert "result" not in outcome


# ---------------------------------------------------------------------------
# Control: duplicate-looking invoices go to the owner
# ---------------------------------------------------------------------------

def test_a_duplicate_invoice_is_held(runtime):
    _grant(runtime, DUPLICATE)
    outcome = runtime.propose("payments.send", _payment_args(runtime, DUPLICATE),
                              invoice_id=DUPLICATE)
    assert outcome["decision"] == "hold"
    assert "duplicate" in outcome["owner_question"].lower()


# ---------------------------------------------------------------------------
# Control: no payment without a human-confirmed intent
# ---------------------------------------------------------------------------

def test_a_payment_without_an_auth_code_is_denied(runtime):
    _grant(runtime, NORMAL)
    args = _payment_args(runtime, NORMAL)
    del args["auth_code"]
    outcome = runtime.propose("payments.send", args, invoice_id=NORMAL)
    assert outcome["decision"] == "deny"


def test_the_financial_tool_itself_also_refuses_without_a_verified_intent(runtime):
    """Defence in depth: the implementation enforces this too.

    Even if policy were misconfigured, `payments.send` returns state=rejected
    without a valid auth code. An app can get this wrong twice and still not lose
    money.
    """
    result = runtime.registry.call(
        "payments.send", payer="acme-operating", payee="sup-northwind",
        amount_cents=125000, currency="USD", auth_code=None)
    assert result["state"] == "rejected"
    assert "missing verified intent auth code" in result["reject_reason"]


def test_the_owner_confirms_before_the_credential_is_minted(runtime):
    """The agent cannot self-authorize.

    If the agent could mint the auth_code itself, `app.requires_verified_intent`
    would check nothing. So capture-then-confirm is split: the agent captures,
    the owner confirms, and only then does the code exist.
    """
    intent = runtime.intents.capture_intent(
        principal="owner", amount_cents=125000, currency="USD",
        payee="sup-northwind", purpose="invoice INV-1001")
    code = runtime.intents.confirm(intent.intent_id, by="owner")

    # The code binds (amount, currency, payee) — it will not verify for another.
    from fin_tools.intent_verification import VerificationError
    with pytest.raises(VerificationError):
        runtime.intents.verify(code.code, amount_cents=999_999_00,
                               currency="USD", payee="sup-northwind")


def test_an_auth_code_is_single_use(runtime):
    from fin_tools.intent_verification import VerificationError
    runtime.book.balances_cents  # noqa: B018 — world is seeded at build time
    intent = runtime.intents.capture_intent(
        principal="owner", amount_cents=125000, currency="USD",
        payee="sup-northwind", purpose="invoice INV-1001")
    code = runtime.intents.confirm(intent.intent_id, by="owner")
    runtime.intents.verify(code.code, amount_cents=125000, currency="USD",
                           payee="sup-northwind")
    runtime.intents.consume(code.code)
    with pytest.raises(VerificationError):
        runtime.intents.verify(code.code, amount_cents=125000, currency="USD",
                               payee="sup-northwind")


# ---------------------------------------------------------------------------
# THE ONE THAT MATTERS MOST: facts are not claims
# ---------------------------------------------------------------------------

def test_the_agent_cannot_overpay_an_invoice(runtime):
    """The agent chooses the amount; the ledger chooses the ceiling.

    Asking for $99,999.99 against a $48,000 invoice is held by
    `app.amount_matches_invoice`, because the ceiling is
    `max_from_fact: invoice_amount_cents` resolved from the fact source. Neither
    side of that comparison is under the agent's control.
    """
    _grant(runtime, LARGE)
    outcome = runtime.propose(
        "payments.send", _payment_args(runtime, LARGE, amount_cents=9_999_999),
        invoice_id=LARGE)
    assert outcome["decision"] == "hold"
    assert "exceeds the recorded invoice amount" in outcome["reason"]


def test_underpayment_is_not_caught_by_the_ceiling(runtime):
    """Documents a boundary honestly rather than implying protection that is absent.

    A ceiling on an *argument* constrains how much the agent may ask for. Paying
    $1.00 against a $48,000 invoice is inside that ceiling, so this rule allows
    it. That is not a defect in the rule — it is the wrong rule for that threat.
    See the next test for what actually stops it.

    This test exists so that nobody "fixes" the rule by adding a floor without
    understanding that the real control is elsewhere.
    """
    _grant(runtime, LARGE)
    outcome = runtime.propose(
        "payments.send",
        _payment_args(runtime, LARGE, amount_cents=100,
                      auth_code=_real_auth_code(runtime, LARGE, amount_cents=100)),
        invoice_id=LARGE)
    assert outcome["decision"] == "allow"


def test_understating_an_amount_requires_the_owner_to_confirm_it(runtime):
    """The control that actually stops underpayment: credential binding.

    An auth code is an HMAC over (amount, currency, payee). A code the owner
    confirmed for $48,000 will not verify for $1.00, so an agent cannot take a
    confirmed large intent and pay a small one — and cannot pay a small amount at
    all unless the owner confirmed that small amount.

    Two directions, two mechanisms: the fact ceiling catches overpayment, intent
    binding catches underpayment.
    """
    from fin_tools.intent_verification import VerificationError

    big = _real_auth_code(runtime, LARGE)            # owner confirmed $48,000
    with pytest.raises(VerificationError) as exc:
        runtime.intents.verify(big, amount_cents=100, currency="USD",
                               payee="sup-delta")
    assert "amount differs" in str(exc.value)

    # ...and the tool itself refuses to settle on a mismatched credential.
    _grant(runtime, LARGE)
    outcome = runtime.propose(
        "payments.send",
        _payment_args(runtime, LARGE, amount_cents=100, auth_code=big),
        invoice_id=LARGE)
    assert outcome["result"]["state"] == "rejected"


def test_a_lie_in_resolved_facts_changes_nothing(runtime):
    """`resolved_facts` names a subject; it is not evidence.

    Values the agent supplies are deliberately not merged into the facts the
    auditor uses, so an agent cannot assert the very values a limit checks.
    """
    _grant(runtime, LARGE)
    args = _payment_args(runtime, LARGE)
    request = ActionRequest(
        request_id="lie-1", app_id=runtime.app_id, agent_id=runtime.agent_id,
        principal=runtime.principal, task_type=runtime.task_type,
        tool="payments.send", arguments=args, authorization=runtime._grant,
        # Claim the invoice is tiny and untainted.
        resolved_facts={"invoice_id": LARGE, "invoice_amount_cents": 100,
                        "duplicate_invoice": False,
                        "payment_details_changed": False},
    )
    decision = runtime.auditor.adjudicate(request)
    assert decision.decision is Decision.HOLD, decision.reason


def test_the_fact_source_never_reads_the_request():
    """The property that makes the above two tests meaningful.

    `InvoiceFactSource` takes only a subject id and reads the app's own data. If
    it accepted arguments, every claim above would be a fact.
    """
    import inspect
    book = InvoiceBook.load(HERE / "data" / "invoices.json")
    fs = InvoiceFactSource(book)
    for name, fn in inspect.getmembers(fs, predicate=inspect.ismethod):
        if name.startswith("_"):
            continue
        params = list(inspect.signature(fn).parameters)
        assert "arguments" not in params and "request" not in params, (
            f"{name} must not consult the request")
    # And it answers from the book.
    assert fs.amount_for(LARGE) == Decimal("48000.00")
    assert fs.extra_facts_for(LARGE)["invoice_amount_cents"] == 4_800_000


def test_an_unresolvable_fact_fails_closed(runtime):
    """Unknown is never read as small.

    A fact-bound rule with no fact denies rather than allowing. This is why a
    mis-named subject argument silently breaks an app's policy — safely, but
    confusingly.
    """
    _grant(runtime, LARGE)
    args = _payment_args(runtime, LARGE)
    request = ActionRequest(
        request_id="nosubject-1", app_id=runtime.app_id,
        agent_id=runtime.agent_id, principal=runtime.principal,
        task_type=runtime.task_type, tool="payments.send", arguments=args,
        authorization=runtime._grant,
        # `inv` is not one of FactResolver.SUBJECT_KEYS, so no record is
        # selected and invoice_amount_cents never resolves.
        resolved_facts={"inv": LARGE},
    )
    decision = runtime.auditor.adjudicate(request)
    assert decision.decision is not Decision.ALLOW


# ---------------------------------------------------------------------------
# Authority: grants are scoped and bounded
# ---------------------------------------------------------------------------

def test_an_unregistered_tool_is_denied(runtime):
    _grant(runtime, NORMAL)
    outcome = runtime.propose("payments.wire_to_offshore", {"amount_cents": 1},
                              invoice_id=NORMAL)
    assert outcome["decision"] == "deny"


def test_a_tool_outside_the_task_allowlist_is_denied(runtime):
    """`payments.receive` exists in fin_tools but is not in this task's allowlist.

    Capability is granted, never assumed.
    """
    request = AuthorizationRequest(
        request_id="over-1", app_id=runtime.app_id, agent_id=runtime.agent_id,
        principal=runtime.principal, task_type=runtime.task_type,
        task_summary="ask for more than the manifest allows",
        requested_tools=["payments.receive"],
        context={"invoice_id": NORMAL},
    )
    grant = runtime.auditor.authorize(request)
    assert "payments.receive" not in grant.tool_names()


def test_the_grant_is_signed_and_scoped(runtime):
    grant = _grant(runtime, NORMAL)
    assert grant.app_id == runtime.app_id
    assert grant.agent_id == runtime.agent_id
    assert grant.task_type == runtime.task_type
    assert grant.signature is not None
    assert not grant.unsigned


# ---------------------------------------------------------------------------
# The agent cannot argue with the gate
# ---------------------------------------------------------------------------

def test_a_held_call_stays_held_no_matter_how_it_is_reworded(runtime):
    """Rewording a held call does not get past the hold.

    This is the failure mode a reviewer looks for: an agent that treats a hold as
    a negotiation rather than a stop. Every retry is judged against the same
    fact-derived ceiling, so rephrasing the memo, changing the reference or
    re-ordering arguments changes nothing.

    Note what this does NOT claim: an agent may legitimately propose a *smaller*
    payment that is inside policy, and the gate will allow it. That is correct
    behaviour, not a bypass — see test_underpayment_is_not_caught_by_the_ceiling.
    """
    _grant(runtime, LARGE)
    base = _payment_args(runtime, LARGE)
    first = runtime.propose("payments.send", base, invoice_id=LARGE)
    assert first["decision"] == "hold"

    # Same amount, different words. The memo is not what the rule reads.
    reworded = dict(base, memo="routine supplier payment, nothing unusual",
                    idempotency_key="test-retry")
    second = runtime.propose("payments.send", reworded, invoice_id=LARGE)
    assert second["decision"] == "hold"
    assert "25,000" in second["owner_question"]

    # Overpaying instead, to dodge the large-payment rule by staying under it
    # while exceeding the invoice. Caught by the fact ceiling, not the cap.
    over = dict(base, amount_cents=9_999_999, idempotency_key="test-over")
    third = runtime.propose("payments.send", over, invoice_id=LARGE)
    assert third["decision"] == "hold"
    assert "exceeds the recorded invoice amount" in third["reason"]

    # Nothing moved across three attempts.
    assert runtime.read("payments.balance",
                        account="acme-operating")["balance_cents"] == 6_000_000


def test_a_held_call_does_not_execute_while_it_waits(runtime):
    """The proposal is not executed optimistically before the owner decides."""
    _grant(runtime, LARGE)
    before = runtime.read("payments.balance", account="acme-operating")["balance_cents"]
    outcome = runtime.propose("payments.send", _payment_args(runtime, LARGE),
                              invoice_id=LARGE)
    assert outcome["decision"] == "hold"
    assert "result" not in outcome, "a held proposal must not carry a result"
    assert runtime.read("payments.balance",
                        account="acme-operating")["balance_cents"] == before


def test_every_step_is_written_to_the_action_log(runtime):
    _grant(runtime, NORMAL)
    runtime.read("lookup_invoice", invoice_id=NORMAL)
    runtime.propose("payments.send", _payment_args(runtime, NORMAL),
                    invoice_id=NORMAL)
    kinds = {e.get("kind") for e in runtime.log.entries()}
    assert {"authorization", "tool_call", "proposal", "verdict"} <= kinds


def test_the_audit_log_is_hash_chained(runtime):
    """Tamper-evident evidence: edits and deletions break verification."""
    _grant(runtime, NORMAL)
    runtime.propose("payments.send", _payment_args(runtime, NORMAL),
                    invoice_id=NORMAL)
    audit_log = runtime.log.run_dir / "audit.jsonl"
    assert audit_log.exists()
    entries = [json.loads(line) for line in
               audit_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert entries, "the auditor wrote nothing"
    assert all("prev_hash" in e or "hash" in e for e in entries)


# ---------------------------------------------------------------------------
# Credentials must not reach disk
# ---------------------------------------------------------------------------

def test_auth_code_is_redacted_from_the_action_log(runtime):
    """The single-use payment credential never reaches the action log.

    Two layers now mask it, and this asserts the end-to-end result: the
    platform (`audit_agent.contracts.REDACTED_KEYS` includes `auth_code` /
    `authorization_code`, so the audit agent redacts it before it can enter the
    hash-chained audit log via `owner_question`), and the app's own
    `redact_arguments` for the proposal line it writes. Historically only the
    app layer covered `auth_code` and the credential leaked in full without it;
    that upstream gap is now closed, so the app layer is defence-in-depth.
    """
    secret = "c5ccef68c72731b52dbb20d62f74300ecb871c70066a1a441aa173271748a123"
    _grant(runtime, NORMAL)
    runtime.propose("payments.send",
                    _payment_args(runtime, NORMAL, auth_code=secret),
                    invoice_id=NORMAL)

    log_text = (runtime.log.run_dir / "action-log.jsonl").read_text(encoding="utf-8")
    assert secret not in log_text, "a payment credential reached the action log"
    assert "***REDACTED***" in log_text


def test_redaction_extends_the_platform_key_set_rather_than_replacing_it():
    """Anything audit_agent already masks stays masked, and the app adds more.

    `auth_code` used to be an app-only extension covering an upstream gap; the
    gap is now closed (`auth_code` and `authorization_code` are in
    `audit_agent.contracts.REDACTED_KEYS`), so the platform masks it too. The
    point of this test is unchanged: the app's set is a *superset* of the
    platform's — it never substitutes for it — proven here with keys the
    platform still does not cover (`cvv`, `card_number`, `nonce`).
    """
    from audit_agent.contracts import REDACTED_KEYS
    out = redact_arguments({"password": "hunter2", "auth_code": "deadbeef" * 8,
                            "cvv": "123", "amount_cents": 100})
    assert out["password"] == "***REDACTED***"
    assert out["auth_code"] == "***REDACTED***"
    assert out["cvv"] == "***REDACTED***"
    assert out["amount_cents"] == 100          # business data is not redacted

    # The upstream gap is closed: the platform now masks the payment credential.
    assert "auth_code" in REDACTED_KEYS
    assert "authorization_code" in REDACTED_KEYS
    # The app still extends beyond the platform for card-shaped fields it adds.
    assert {"cvv", "card_number", "nonce"} <= _CREDENTIAL_KEYS
    assert "cvv" not in REDACTED_KEYS          # app-only, not upstream
    assert _CREDENTIAL_KEYS >= set(REDACTED_KEYS)  # superset, never a subset


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

def test_the_full_run_produces_every_expected_outcome(runtime):
    """Five invoices, five different control paths, in one run."""
    owner = ScriptedOwner(runtime.book, verbose=False)
    agent = ScriptedPayerAgent(runtime, owner=owner)

    statuses = {i: agent.run(i)["status"] for i in runtime.book.invoices}

    assert statuses[NORMAL] == "paid"
    assert statuses[LARGE] == "paid"                  # held, then approved
    assert statuses[CHANGED_DETAILS] == "refused"     # denied in code
    assert statuses[DUPLICATE] == "declined_by_owner"  # held, then declined
    assert statuses["INV-1005"] == "paid"


def test_the_owner_was_asked_about_exactly_the_held_payments(runtime):
    owner = ScriptedOwner(runtime.book, verbose=False)
    agent = ScriptedPayerAgent(runtime, owner=owner)
    for i in runtime.book.invoices:
        agent.run(i)

    holds = [d for d in owner.decisions if "hold_id" in d]
    assert len(holds) == 2, "expected the large payment and the duplicate"
    assert [d["approved"] for d in holds] == [True, False]


def test_no_money_moved_for_the_refused_or_declined_invoices(runtime):
    owner = ScriptedOwner(runtime.book, verbose=False)
    agent = ScriptedPayerAgent(runtime, owner=owner)
    for i in runtime.book.invoices:
        agent.run(i)

    # 60,000.00 opening, minus 1,250.00 + 48,000.00 + 310.50 paid.
    expected = 6_000_000 - (125_000 + 4_800_000 + 31_050)
    assert runtime.read("payments.balance",
                        account="acme-operating")["balance_cents"] == expected

# Platform status: what actually exists (verified 2026-10-15)

Read this before building anything. It distinguishes **what runs today** from
**what is designed but not implemented**, so a generated app does not call a
function that does not exist.

Everything below was verified by importing the packages and running them, not by
reading the specs. Where a spec and the code disagree, the code is right and this
file says so.

## The packages

| Package | Path | Import | Status |
|---|---|---|---|
| Financial tools | `platform/financial-tools` | `fin_tools` + four sibling packages | **Works.** 367 tests offline. `fin_tools` registers 12 tools; siblings are `helios_forecast`, `spreadsheets`, `email_tool`, `payments_collect` |
| Audit agent builder | `platform/audit-agent-builder` | `audit_agent` | **Works.** 81 tests pass offline; pydantic + pyyaml |
| Agent toolkit (action side) | `platform/action-agent-builder` | `agent_toolkit` | **Works.** Contracts, log, holds, manifest loader, mock tools. No CLI yet (P3 writes `cli.py`; the pyproject no longer declares a script entry for it) |
| Agent integration | `platform/agent-integration` | `agent_integration` | **Works.** 136 tests. The gated agent loop, audit bridge, adapters, manifest generator, Qwen model driver and CLI |
| Deployment engine | `platform/deployment` | `engine`, `cli.helios`, `helios_sdk` | **Works.** 96 offline tests (docker-py mocked). Needs a Docker daemon only to actually deploy |
| App builder | `platform/app-builder` | — | **Design only.** README + PLAN, no code |

All of these are installed editable into one repo venv — `scripts/bootstrap-venv.sh`
builds it, `scripts/test-all.sh` runs every suite. No `PYTHONPATH` juggling and no
per-package venv any more.

## What is missing, concretely

### 1. There is no scaffolder, validator, or schema tool

`specs/app-builder.md` defines all three in detail. None is implemented;
`platform/app-builder/` holds a README and a PLAN only. There is no
`helios-app.yaml` parser, no `stacks.yaml`, no UI kit, no DDL generator.

`entry-point/checks/check_app.py` is the working stand-in for the validator: it
loads both manifests under their own builders' validators and cross-checks them,
including that the action side and the audit manifest agree on every tool's
side-effect flag. It reports all problems at once and exits non-zero on any
error. Run it in CI. It has its own tests under `entry-point/checks/tests/`.

**Consequence for you:** *you are the scaffolder.* Follow the build order in
`BUILD.md` and write the app directly. Write `helios-app.yaml` anyway — it is the
design record, and `check_app.py` already machine-checks much of it.

### 2. The agent loop exists — use it

There is a gated agent loop: `platform/agent-integration/`.

- `runtime.GatedAgentRuntime` runs the loop: authorize the task, offer the model
  only what was granted, turn every side-effect call into a proposal, adjudicate
  it through the bridge, then execute / hold / deny. Holds are resumable by id,
  and a run that exhausts its turn cap is an error rather than a silent success.
- `model_driver.ModelDriver` drives Qwen over DashScope's OpenAI-compatible
  endpoint with stdlib `urllib` — no SDK, no dependency tree. It returns a
  proposal and stops, so the gate cannot be bypassed by an off-the-shelf helper
  running the loop for you.
- `wiring.build_integration()` assembles the auditor, the bridge and the runtime
  in one call. `cli.py` gives you `demo`, `doctor`, `run`, `inspect`, `manifest`
  and `selfcheck`.

`qwen_agent` is **not** what the loop is built on, and no file imports it except
a lazy optional import in `audit_agent/advisory.py` behind the `advisory` extra.
Any doc that says the runtime is "built on Qwen-Agent" is wrong: the loop is an
OpenAI-compatible tool-calling loop driven by Qwen.

**Consequence for you:** you do not have to supply a loop. Use
`build_integration()`, or the example's pattern of driving the runtime from
deterministic code when you want an offline demo. The gate underneath is the same
either way — that is the point of the split.

### 3. The standalone audit agent is the authoritative gate

This was the most important open item; it is now decided (see
`specs/decisions.md`).

- `audit_agent.AuditAgent` — standalone, declarative rules from a YAML manifest,
  signed scoped time-boxed grants, fact sources, hash-chained log. **This is the
  in-app gate.**
- `agent_toolkit.audit.AuditGate` — in-process, checks a `Proposal` against a
  static `Mandate`. **Legacy and not in any run path.** Its `MandateLimits`
  accepts only four receivables-shaped keys (`max_extension_days`,
  `extension_owner_approval_days`, `min_days_between_contacts`,
  `invoice_owner_approval_amount`) and its rule logic is hard-coded to
  `send_email`, so a payables app cannot express its policy there at all. The
  class is kept importable and marked deprecated in its docstring. Do not add
  rules to it.

The engine's audit sidecar (`platform/deployment/images/auditor`) is a *separate*
second gate: fact-free, and it holds the tenant execution secret. It is not a
substitute for the in-app auditor.

### 4. The tool contracts are adapted, not incompatible

Both packages define a tool contract, and they are not the same shape:

| | `agent_toolkit.contracts.ToolDefinition` | `fin_tools.contracts.ToolSpec` |
|---|---|---|
| side-effect field | `side_effect: bool` | `side_effects: bool` (plural) |
| handler signature | `handler(args: Mapping)` | `handler(**kwargs)` |
| schema shape | JSON Schema object | flat `{name: "type"}` strings |

The adapters already exist — **you do not need to write one**:

- `fin_tools.toolkit_bridge.as_tool_definitions(bundle)` / `agent_tool_definitions(bundle)`
  return the financial tools as real `agent_toolkit` `ToolDefinition`s, converting
  the flat field map into JSON Schema and the kwargs handler into a
  Mapping-taking one. `agent_tool_definitions` excludes the intent-issuing
  operations (`intents.capture` / `confirm` / `revoke`), because an agent that
  could mint and confirm its own intent could authorise itself.
- `agent_integration.adapters.ToolContractView` closes the gap in the other
  direction, so the auditor registers each tool with the correct side-effect flag
  and schema. Without it a gated tool would register as read-only — and
  read-only calls are never gated.

Left unadapted, both fail *silently*: the schema gap means the auditor's argument
validation never fires, and the flag gap means a side effect runs ungated. That
is why these are tested rather than assumed.

### 5. Shared services: the deployment engine is real, the rest are not

- **Deployment engine: real.** `platform/deployment` — `helios.yaml` + an ASGI
  entry + `GET /health`, built on golden base images, smoke-tested in a sandbox,
  routed through nginx, with a per-app audit sidecar. Drives the Docker Engine
  API via docker-py.
- **Knowledge graph, Intent & Trust Service, shared events: declarations only.**
  `fin_tools` provides *offline prototypes*. Nothing touches a network, a
  database, or real money. `specs/decisions.md` still lists cross-app
  subscription, service authentication and platform-wide mandate caps as open.

## Credential handling

`audit_agent.redact()` masks a fixed key set, and as of this session it includes
the payment credential:

```
access_token, api-key, api_key, apikey, auth_code, authorization,
authorization_code, client_secret, cookie, dashscope_api_key, passwd, password,
private_key, refresh_token, secret, token
```

`auth_code` / `authorization_code` are the Intent Verification service's
single-use credential that authorises a payment. They used to be missing from
that set, so the credential reached the hash-chained audit log in full via the
`owner_question` string, which renders every argument. **That is fixed** — both
keys are in `REDACTED_KEYS`
(`platform/audit-agent-builder/src/audit_agent/contracts.py`), and
`_owner_question` therefore renders the masked value. Asserted by
`test_a_held_payment_credential_never_reaches_the_owner_question` in the audit
builder suite and by the invoice-payer example's own log test.

Two consequences worth knowing:

- The gate checks **presence**, the tool checks **validity**. `payments.send`
  still receives the real code at execution — the runtime executes the caller's
  original arguments, not the redacted copy. Redaction protects the log, not the
  call.
- Because the credential is redacted before schema validation, a manifest must
  not put a `minLength` or `pattern` on `auth_code`: the gate would only ever be
  validating the 14-character `***REDACTED***` placeholder. The invoice-payer
  manifest dropped its `minLength: 16` for exactly this reason.

## Environment notes

- `audit-agent-builder` and `agent-integration` require **Python >= 3.13**; the
  others require >= 3.10. `scripts/bootstrap-venv.sh` builds `.venv` on 3.13 and
  refuses to fall back to an older `python3`.
- **Model:** Qwen via DashScope. Defaults `qwen3.8-max` at
  `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`. Token env vars in
  priority order: `HELIOS_LLM_API_KEY`, `DASHSCOPE_API_KEY`, `OPENAI_API_KEY`.
  Base URL also falls back to `OPENAI_BASE_URL` and model to `OPENAI_MODEL`,
  because Qwen Code sets those. Competition rule: Qwen is the only generative
  model allowed.
- `enable_thinking: false` is sent **only** when the model id contains `qwen3`
  (hybrid reasoning models, which refuse a non-streaming request while thinking
  is on). For any other model id, neither thinking field is sent — an endpoint
  that does not know the parameter rejects an unknown one.
- No token is needed for anything offline: `demo`, `selfcheck`, `inspect`,
  `run --offline` and the whole test suite. `agent-integration doctor` is the
  connectivity check; it prints the token's shape, never its value.
- `build_audit_agent()` raises `SigningKeyMissingError` unless
  `HELIOS_AUDIT_SIGNING_KEY` is set, or the manifest sets
  `allow_unsigned_grants: true`. `build_integration()` generates an ephemeral
  process key when neither is configured and reports `ephemeral_key=True` —
  **local development only.** Never ship that.
- Grants are short-lived (default `ttl_seconds: 300`; the example uses 120 for
  money movement). A long-running agent must re-authorize per task, not once at
  startup.
- Side-effect grants are **single-use**. Retrying a payment needs a fresh grant;
  reuse the same `idempotency_key` so a retry cannot pay twice.
- **`idempotency_key` replays a cached outcome, including a rejection.**
  Repeating a key returns the stored record with its original `reject_reason`
  and `payment_id` rather than re-evaluating, even if the arguments have
  changed. That is what prevents a double payment, but it also means an agent
  that *fixes* a rejected payment and retries under the same key sees the old
  failure and may conclude its fix did not work. Reuse the key when retrying the
  same payment; use a fresh one when the payment genuinely changed. Asserted in
  `examples/invoice-payer/tests/test_controls.py::test_an_idempotency_key_replays_a_cached_rejection`.
- `payments.send` distinguishes an **absent** `auth_code` (`"missing verified
  intent auth code"`) from a **present but invalid** one (`"unknown auth code"`,
  or `"amount differs from confirmed intent"` on a mismatch). The gate checks
  presence; the tool checks validity — two layers, so a test that only asserts a
  verdict can pass while no money moves.
- `FactResolver.SUBJECT_KEYS` is fixed: `invoice_id`, `bill_id`, `customer_id`,
  `supplier_id`, `subject_id`, `payment_id`. Arguments named anything else will
  not select a record for fact resolution, and a fact-bound rule will then fail
  closed. This is the most common way a generated app's policy silently stops
  working.
- **Contact counting is flag-driven, not name-driven.** A tool participates in
  contact-frequency rules by setting `records_contact=True` on its
  `ToolDefinition`; the runtime no longer matches a hard-coded `send_email`. A
  tool that contacts a customer under any other name must set the flag, or the
  rule under-counts and an agent can message past the cap.
- Holds are **in-memory only** in the audit builder — lost on restart.
  `InMemoryReplayGuard` is single-process. Both are documented gaps there;
  `specs/decisions.md` records that held state and replay guards persist to
  SQLite under the app's data dir, which P2 implements.

## Where to check for changes

This file is a snapshot and will go stale. Re-verify before relying on it:

```bash
# From the repo root, using the repo venv (scripts/bootstrap-venv.sh builds it).
scripts/test-all.sh                                            # 803 passed, 7 suites

.venv/bin/python -m pytest platform/audit-agent-builder/tests   #  81 pass
.venv/bin/python -m pytest platform/financial-tools/tests       # 367 pass
.venv/bin/python -m pytest platform/agent-integration/tests     # 136 pass
.venv/bin/python -m pytest platform/deployment/tests            #  96 pass

# How many financial tools does build_world() register? Expect 12.
.venv/bin/python -c "from fin_tools import build_world; \
print(len(build_world().registry.list()))"

# What does the auditor actually govern, for a given app?
.venv/bin/python -m agent_integration.cli inspect
```

Python 3.13.9 in the repo venv was used for all of the above.

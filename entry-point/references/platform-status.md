# Platform status: what actually exists (verified 2026-10-14)

Read this before building anything. It distinguishes **what runs today** from
**what is designed but not implemented**, so a generated app does not call a
function that does not exist.

Everything below was verified by importing the packages and running them, not by
reading the specs. Where a spec and the code disagree, the code is right and this
file says so.

## The three packages

| Package | Path | Import | Status |
|---|---|---|---|
| Financial tools | `platform/financial-tools` | `fin_tools` | **Works.** 12 registered tools, offline, no deps beyond stdlib |
| Audit agent builder | `platform/audit-agent-builder` | `audit_agent` | **Works.** 79 tests pass offline; pydantic + pyyaml |
| Agent toolkit (action side) | `platform/action-agent-builder` | `agent_toolkit` | **Partial.** Gate, log, holds, manifest loader work; **no agent loop, no CLI** |
| App builder | `platform/app-builder` | — | **Design only.** README placeholder; no code |

All three import cleanly from a single process with the source directories on
`sys.path`. That is how `examples/invoice-payer/app.py` runs.

## What is missing, concretely

### 1. There is no agent loop

`specs/architecture-mermaid.md` says the runtime provides "the model calls tools
in a loop, built on Qwen-Agent". It does not yet:

- `agent_toolkit` has no `cli.py`, although `pyproject.toml` declares an
  `agent-toolkit` script entry point at `agent_toolkit.cli:main`. That script
  will fail to install as written.
- `qwen_agent` is declared as a dependency but **is not installed** in this
  checkout.
- No `DASHSCOPE_API_KEY` is configured.

**Consequence for you:** you must supply the loop. Either write it against
Qwen-Agent once it is installed, or (as the example does) drive the runtime from
deterministic code so the app runs offline. The gate underneath is unchanged
either way — that is the point of the split.

### 2. There is no scaffolder, validator, or schema tool

`specs/app-builder.md` defines all three in detail. None is implemented;
`platform/app-builder/` holds a README only. There is no `helios-app.yaml`
parser, no `stacks.yaml`, no UI kit, no DDL generator.

**Consequence for you:** *you are the scaffolder.* Follow the build order in
`BUILD.md` and write the app directly. Write `helios-app.yaml` anyway — it is the
design record and it will be machine-checkable the moment the validator lands.

### 3. The two audit gates overlap, and nobody has decided which wins

This is the most important open item for anyone building an app.

- `agent_toolkit.audit.AuditGate` — in-process, checks a `Proposal` against a
  static `Mandate`. Its `MandateLimits` accepts **only four keys**, all
  receivables-shaped: `max_extension_days`, `extension_owner_approval_days`,
  `min_days_between_contacts`, `invoice_owner_approval_amount`. Its rule logic is
  hard-coded to the `send_email` tool.
- `audit_agent.AuditAgent` — standalone, declarative rules from a YAML manifest,
  signed scoped grants, fact sources, hash-chained log.

**A payables app cannot express its policy in the in-process gate at all.** There
is no way to declare "payment may not exceed the recorded invoice amount" or
"deny when supplier details changed" in `MandateLimits`.

`examples/invoice-payer/app.py` resolves this by making the standalone auditor
authoritative and using the in-process gate for nothing. That is a *defensible
choice, not a settled design* — it is the open question recorded in
`platform/audit-agent-builder/README.md`, and `AGENTS.md` requires team approval
for architecture changes. Do not silently bake it into your app; flag it.

### 4. The two packages have incompatible tool contracts

Both define a tool contract, and they are not the same shape:

| | `agent_toolkit.contracts.ToolDefinition` | `fin_tools.contracts.ToolSpec` |
|---|---|---|
| side-effect field | `side_effect: bool` | `side_effects: bool` (plural) |
| handler signature | `handler(args: Mapping)` | `handler(**kwargs)` |
| schema shape | JSON Schema object | flat `{name: "type"}` strings |

**Consequence:** the financial tools cannot be dropped into `agent_toolkit`'s
registry without an adapter. The example sidesteps this by calling
`registry.call(name, **kwargs)` directly and gating on the name list instead of
the `side_effect` flag. If you build an app that uses both packages for real,
write the adapter — and say so, because it is a platform gap worth closing.

### 5. Shared services are declarations only

The knowledge graph, Intent & Trust Service, shared events, and deployment engine
do not exist as services. `fin_tools` provides *offline prototypes* of payments,
email, graph and intent verification. Nothing touches a network, a database, or
real money. `specs/decisions.md` still lists hosting, deployment artifacts and
service authentication as open questions.

## Credential handling: a real gap you must work around

Verified 2026-10-14. `audit_agent.redact()` masks a fixed key set:

```
access_token, api-key, api_key, apikey, authorization, client_secret, cookie,
dashscope_api_key, passwd, password, private_key, refresh_token, secret, token
```

**`auth_code` is not in that set.** The Intent Verification service's `auth_code`
is the single-use credential that authorises a payment, and when it is passed as
a tool argument it is written to the audit log **in full**, inside the
`owner_question` string that renders every argument.

This cannot be worked around by not sending it: a rule like
`app.requires_verified_intent` must check `required_arguments: [auth_code]`, and
`payments.send` must receive the real code to execute. So the credential
genuinely has to flow through the proposal — which means **the auditor's log has
to redact it**. That is an upstream fix, not an app-level one.

What the example does meanwhile:

- Redacts `auth_code` (plus a few other credential shapes) before writing its own
  action log, extending `REDACTED_KEYS` rather than replacing it.
- Leaves the audit-side leak in place, because `platform/audit-agent-builder/` is
  not ours to edit.

**Recommended fix for whoever owns that package** (both files verified to exist at
these paths on 2026-10-14):

- Add `auth_code` and `authorization_code` to `REDACTED_KEYS`, defined at
  `platform/audit-agent-builder/src/audit_agent/contracts.py:54`. It is a
  `frozenset` of lowercased key names matched against argument keys, so the
  addition is one line.
- Redact argument values when composing the owner-facing text, in
  `_owner_question` at
  `platform/audit-agent-builder/src/audit_agent/agent.py:705`. That method renders
  every argument into the question string, which is where the credential
  currently leaks — redacting on the way to disk is not enough if the same value
  is shown to a human through a channel that also logs it.

Adding the key to `REDACTED_KEYS` closes the disk leak on its own and is the
higher-value of the two.

**Until then:** do not let a generated app put a real credential through a
proposal that reaches the audit log. Prefer a short-lived reference the auditor
can check for presence, and resolve the credential at execution time.

## Environment notes

- `audit-agent-builder` requires **Python >= 3.13**; the other two require
  >= 3.10. Use one interpreter that satisfies the strictest (3.13+).
- `build_audit_agent()` raises `SigningKeyMissingError` unless
  `HELIOS_AUDIT_SIGNING_KEY` is set, or the manifest sets
  `allow_unsigned_grants: true`. The example sets a dummy key via
  `os.environ.setdefault` — **local development only.** Never ship that.
- Grants are short-lived (default `ttl_seconds: 300`; the example uses 120 for
  money movement). A long-running agent must re-authorize per task, not once at
  startup.
- Side-effect grants are **single-use**. Retrying a payment needs a fresh grant;
  reuse the same `idempotency_key` so a retry cannot pay twice.
- **`idempotency_key` replays a cached outcome, including a rejection.** Verified
  2026-10-14: repeating a key returns the stored record with its original
  `reject_reason` and `payment_id` rather than re-evaluating, even if the
  arguments have changed. That is what prevents a double payment, but it also
  means an agent that *fixes* a rejected payment and retries under the same key
  sees the old failure and may conclude its fix did not work. Reuse the key when
  retrying the same payment; use a fresh one when the payment genuinely changed.
  Asserted in `examples/invoice-payer/tests/test_controls.py::test_an_idempotency_key_replays_a_cached_rejection`.
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
- Holds are **in-memory only** in the audit builder — lost on restart.
  `InMemoryReplayGuard` is single-process. Both are documented gaps there.

## Where to check for changes

This file is a snapshot and will go stale. Re-verify before relying on it:

```bash
# Neither package is pip-installed in this checkout, so put src on the path.
cd platform/audit-agent-builder && PYTHONPATH=src python3 -m pytest tests -q   # 79 pass
cd platform/financial-tools     && python3 -m pytest tests -q                  # 44 pass

python3 -c "import sys; sys.path.insert(0,'platform/financial-tools'); \
from fin_tools import build_world; print(len(build_world().registry.list()))"  # expect 12
```

Python 3.13.9 was used for all of the above.

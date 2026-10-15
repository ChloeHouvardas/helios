# Platform status: what actually exists (verified 2026-10-15)

Read this before building anything. It distinguishes **what runs today** from
**what is designed but not implemented**, so a generated app does not call a
function that does not exist.

Everything below was verified by importing the packages and running them, not by
reading the specs. Where a spec and the code disagree, the code is right and this
file says so.

The previous revision of this file was dated 2026-10-14 and claimed there was no
agent loop, no scaffolder, no validator and no `agent_toolkit` CLI. **All four now
exist.** Sections 1 and 2 below were rewritten on 2026-10-15 to say what is
actually there; sections 3-5 and the credential gap are unchanged because those
problems are unchanged.

## The five packages

| Package | Path | Import | Status |
|---|---|---|---|
| Financial tools | `platform/financial-tools` | `fin_tools` | **Works.** 12 registered tools, offline, stdlib-only |
| Audit agent builder | `platform/audit-agent-builder` | `audit_agent` | **Works.** 79 tests pass offline; pydantic + pyyaml |
| Agent toolkit (action side) | `platform/action-agent-builder` | `agent_toolkit` | **Works.** Gate, log, holds, manifest loader, **and a CLI** (`inspect`, `validate`, `demo`). Still no model-driven loop *inside this package* — that is `agent_integration`'s job |
| Agent integration | `platform/agent-integration` | `agent_integration` | **Works.** 123 tests pass. The gated loop, the audit bridge, and `build_integration()` |
| App builder | `platform/app-builder` | `app_builder` | **Works.** Spec model, validator, `stacks.yaml`, schema tool, UI kit, scaffolder, intake, `helios-app` CLI |

All of them import cleanly from a single process. `scripts/bootstrap-venv.sh`
installs the four that are packaged and puts `financial-tools` on the path (see
Environment notes for why that one is different).

## What runs, and what still does not

### 1. There is an agent loop

`agent_integration.wiring.build_integration(...)` returns an `Integration` holding
a `GatedAgentRuntime`, an `AuditBridge` and a standalone auditor, already connected.
That is the loop `specs/architecture-mermaid.md` describes. Both reference apps
compose through it — see `apps/helios-collect/app.py:148-234`.

Three CLIs are installed and working:

```bash
agent-integration --help     # drive the runtime
agent-toolkit --help         # inspect / validate / demo the action side
helios-app --help            # questions / spec-from-intake / new / validate
```

`agent-toolkit` was declared in `pyproject.toml` from the start but `cli.py` did not
exist, so the installed script crashed with `ModuleNotFoundError` on every
invocation. That is fixed.

**One caveat, verified 2026-10-15:** the live-model path is wired (Qwen via
DashScope's OpenAI-compatible endpoint, key in `DASHSCOPE_API_KEY`) but every
demo and every test drives the runtime with the deterministic scripted driver.
The gate underneath is identical either way, which is the point of the split;
run `agent_integration.cli doctor` with a real key before claiming a live run.

### 2. The app builder is implemented

`platform/app-builder/` is no longer a README. It provides:

| Piece | What it does |
|---|---|
| `app_builder.spec` | Pydantic models for `helios-app.yaml`, `extra="forbid"`, every problem reported at once |
| `app_builder.validate` | `validate_app(dir) -> Report(errors, warnings, exit_code)`, shape plus cross-file |
| `app_builder.stacks` | Loads `stacks.yaml` |
| `app_builder.schema` | `data.local` -> SQLite DDL, a generated `store.py`, and fixture seeding |
| `app_builder.kit` | Jinja2 templates, CSS tokens, vendored htmx, the held-action card |
| `app_builder.scaffold` | Spec -> 23-file app skeleton. Never overwrites; idempotent |
| `app_builder.intake` | The 16 questions, and a first spec built from the answers |
| `app_builder.cli` | `helios-app questions | spec-from-intake | new | validate` |

`stacks.yaml` exists and defines `helios-python-web-1`, so the `stack.use` value
both reference apps declare now resolves to something.

`entry-point/checks/check_app.py` still works and still exits 0/1/2 with `--strict`,
but it is now a 56-line wrapper over `app_builder.validate`. Prefer
`helios-app validate <dir>`.

**The stack is not what the spec says.** `specs/app-builder.md` specifies Vite-built
React. The implementation is FastAPI + SQLite (stdlib `sqlite3`, no ORM) + Jinja2 +
HTMX, per the decision that a generated app must be buildable and debuggable by a
model in minutes — and a Node toolchain is neither. Server-rendered pages give the
property the spec actually wanted from Vite: one process, one port, one origin, so
the approval flow stays a same-origin call.

### 3. What is still missing

- **Deployment exists and is in-tree.** `platform/deployment/` builds, smoke-tests,
  attaches an audit sidecar and routes an app directory; `helios-app new` writes its
  `helios.yaml` from the engine's real `Manifest` (eight fields, unknown fields
  rejected, eight permission families). Not yet done there: the tenant core still
  runs a mock tool service, holds are in-memory, and logs are per app.
- **There is no `helios` console script.** Deploy with
  `.venv/bin/python cli/helios.py deploy <dir> [--tenant X]`.
- The knowledge graph, Intent & Trust Service and shared events are still
  declarations, with `fin_tools` providing offline prototypes. Nothing touches a
  network, a database, or real money.
- The graph's **subscription** transport is unsettled. `fin_tools`'
  `KnowledgeGraph` has `upsert_verified(kind, key, attrs, *, source)` and
  `add_observation(kind, key, attrs, *, observer)`, and the registry exposes a
  `graph.publish_verified` *tool* — but there is no subscribe. Generated
  `graph.py` files emit a `subscribe_<entity>` stub that raises and says so.

### 4. The two audit gates overlap, and nobody has decided which wins

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

Both reference apps resolve this by making the standalone auditor authoritative and
using the in-process gate for nothing. That is a *defensible choice, not a settled
design* — it is the open question recorded in
`platform/audit-agent-builder/README.md`, and `AGENTS.md` requires team approval
for architecture changes. Do not silently bake it into your app; flag it.

### 5. The two packages have incompatible tool contracts

Both define a tool contract, and they are not the same shape:

| | `agent_toolkit.contracts.ToolDefinition` | `fin_tools.contracts.ToolSpec` |
|---|---|---|
| side-effect field | `side_effect: bool` | `side_effects: bool` (plural) |
| handler signature | `handler(args: Mapping)` | `handler(**kwargs)` |
| schema shape | JSON Schema object | flat `{name: "type"}` strings |

**Consequence:** the financial tools cannot be dropped into `agent_toolkit`'s
registry without an adapter. Use `fin_tools.toolkit_bridge.agent_tool_definitions(bundle)`
or `adapters.ToolContractView`.

A gated tool that registers as **read-only** because of this mismatch is never
adjudicated — the app emails customers with no audit at all, and nothing raises.
`ToolDescriptor.from_contract()` reads `.has_side_effect` while `ToolDefinition`
names it `.side_effect`, so duck-typing defaults the missing attribute to `False`.
Run `selfcheck` after any change to either package's tool contract.

`ToolDefinition`'s fields are exactly `name`, `description`, `input_schema`,
`output_schema`, `side_effect`, `handler`. **`output_schema` is required and must be
non-empty** — `__post_init__` refuses otherwise. There is **no** `records_contact`
field, despite earlier notes suggesting one was coming.

### 6. Shared services are declarations only

`specs/decisions.md` still lists hosting, deployment artifacts and service
authentication as open questions.

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

- **Use the repo venv**: `.venv/bin/python`, which is 3.13. The system `python3`
  is **3.14** and has none of the dependencies installed. Run
  `scripts/bootstrap-venv.sh` once; it creates `.venv`, installs the four packaged
  packages editable, and puts `financial-tools` on the path.
- `audit-agent-builder` and `app-builder` require **Python >= 3.13**; the others
  require >= 3.10. One interpreter satisfying the strictest is what the venv is.
- **`platform/financial-tools` cannot be installed with `pip install -e`.** It is a
  flat layout (`fin_tools/`, `tests/`, `fixtures/` as siblings) with no
  `[build-system]` section, so setuptools package discovery fails. `bootstrap-venv.sh`
  writes a `.pth` file into the venv instead, which is what an editable install
  would have produced. That is P1's package; do not "fix" it from here.
- `build_audit_agent()` raises `SigningKeyMissingError` unless
  `HELIOS_AUDIT_SIGNING_KEY` is set, or the manifest sets
  `allow_unsigned_grants: true`. `build_integration()` generates an **ephemeral**
  key when the env var is absent, so grants stay signed and the demo cannot be
  shipped by accident. The reference example sets a dummy key via
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
  working. `helios-app validate` warns when a gated tool's schema contains none of
  them.
- Holds are **in-memory only** in the audit builder — lost on restart.
  `InMemoryReplayGuard` is single-process. Both are documented gaps there.
  `agent_toolkit.holds.HoldStore` *does* persist to
  `.runs/<run_id>/holds/<proposal_id>.json`, so an app that saves holds through it
  survives a restart; the audit-side ones do not.
- **`RunResult.summary()` miscounts side effects.** It reports
  `side_effects_attempted` / `side_effects_executed` as `len(self.actions)` /
  `len(self.executed)`, but read-only tool calls are also appended to
  `result.actions`, so both counters include non-side-effect tools. The gate behaved
  correctly; **the reported number is wrong.** The action log is the accurate
  record. Fix before any judging context, because it reads as "an email went out"
  when none did.

## Where to check for changes

This file is a snapshot and will go stale — it already has once. Re-verify before
relying on it:

```bash
scripts/bootstrap-venv.sh          # once
scripts/test-all.sh                # every suite, with per-suite counts
```

As of 2026-10-15 that reports 6 suites, all green:

| suite | result |
|---|---|
| action-agent-builder | 36 passed |
| audit-agent-builder | 79 passed |
| agent-integration | 123 passed |
| financial-tools | 86 passed |
| app-builder | 386 passed |
| helios-collect | 74 passed |

Two checks worth running by hand:

```bash
.venv/bin/python -c "from fin_tools import build_world; \
print(len(build_world().registry.list()))"          # expect 12

.venv/bin/helios-app validate entry-point/examples/invoice-payer   # expect exit 0
.venv/bin/helios-app validate apps/helios-collect                  # expect exit 0
.venv/bin/agent-toolkit demo --run-dir /tmp/demo                   # four outcomes
```

`test-all.sh` passes `--override-ini="addopts="` to pytest. Four packages declare
`addopts = "-q"` in their own `pyproject.toml`, and adding `-q` on the command line
makes it `-qq`, which suppresses the "N passed" summary — so the runner would report
green with no counts at all.

Python 3.13.9 was used for all of the above.

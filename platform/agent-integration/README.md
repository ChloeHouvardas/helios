# agent-integration

The seam between `action-agent-builder` and `audit-agent-builder`, plus the
agent loop Build 1 specified but neither package shipped.

One command, no API key:

```bash
uv pip install -e .          # or: pip install -e .
python -m agent_integration.cli demo
```

## What this is

`action-agent-builder` owns the action side: tool contract, mock tools, store,
manifests, action log. `audit-agent-builder` owns the audit side: signed
grants, deterministic fail-closed policy, hash-chained evidence. Neither
imports the other — deliberately, since the audit agent is only trustworthy
because the app it audits cannot modify it.

The audit README flags the resulting overlap as *"an architecture decision,
not an implementation detail"* and proposes the resolution: the in-app gate
becomes a **client** of the standalone auditor. That is what lives here. This
is the only directory in the repo allowed to import both packages.

```
action agent ──ToolContractView──▶ AuditBridge ──▶ audit agent (registry + policy)
     ▲                                  │                      │
     │            allowed_tools         │   Grant (HMAC)       │
     └──────────────────────────────────┴──────────────────────┘
                                        │
                     every side effect ─▶ adjudicate ─▶ approve / hold / reject
```

## Commands

| Command | What it does |
| --- | --- |
| `demo` | Runs five scenarios offline and prints every verdict |
| `doctor` | Checks model connectivity with one minimal request — run this first |
| `run --task "..."` | One agent on one task. GLM if a token is set, scripted otherwise |
| `run --offline --task "..." --call 'send_email:{...}'` | Scripted, no token needed |
| `inspect` | The authority surface: which tools are gated, which are read-only |
| `manifest [--out path]` | Print or write the generated audit YAML |
| `selfcheck` | Prove the two packages agree — names, side-effect flags, schemas |

Exit codes: `0` ran, `1` config/connectivity, `2` refused (auditor granted no
authority), `3` held with nobody to decide, `4` the run errored.

## Using the real model

GLM-5.3 over the OpenAI-compatible Chat Completion protocol, stdlib HTTP only
— no SDK, no dependency tree.

```bash
cp .env.example .env      # set HELIOS_LLM_API_KEY
set -a; source .env; set +a
agent-integration doctor  # one minimal request, tells you exactly what is wrong
agent-integration run --task "Follow up on overdue invoice INV-1001"
```

`doctor` prints the token as `L2...1Y (13 chars)` — the shape, never the value.

Two things about the GLM-5.3 contract that are easy to get wrong, both handled
in `model_driver.py` and asserted by tests:

- **Auth is a plain Bearer token.** GLM-5.3 rejects the legacy JWT-shaped key,
  so nothing signs or encodes the token.
- **Thinking cannot be disabled.** `reasoning_effort` (`low`/`high`/`max`)
  picks the depth; a request sending `thinking.type: "disabled"` fails outright.
  `tool_choice` only accepts `"auto"`, and `temperature` caps at `1.0`.

If your token came from a gateway rather than Zhipu directly — a token shaped
like `L2...` instead of `sk-...` usually means it did — set
`HELIOS_LLM_BASE_URL` to that gateway, or pass `--base-url`. The public
endpoints are `https://open.bigmodel.cn/api/paas/v4` (mainland) and
`https://api.z.ai/api/paas/v4` (international).

When the auditor holds an action the CLI prompts you:

```
  HELD     send_email  (hold id: hold_7f3a...)
           Invoice amount 12500.00 exceeds the 10000 owner threshold.
           approve this exact action? [y/N]
```

Say `y` and the runtime resumes with that single action and nothing else.
Say `n` and the denial reason goes back to the model as its next observation.

Without a token nothing changes except the driver: `run` warns and falls back
to scripted.

## Three gaps this closes

**1. A gated tool would have registered as read-only.**
`ToolDescriptor.from_contract()` reads `.has_side_effect`; the action side's
`ToolDefinition` has `.side_effect`. Duck-typing those together does not raise
— it defaults to `side_effect=False`. `send_email` would have been handed
straight to the model ungated. `ToolContractView` closes this, and `selfcheck`
fails loudly if it ever reopens.

**2. The tool names never matched.**
Action side: `send_email(customer_id, invoice_id, subject, body,
extension_offer_days)`. Audit manifest: `send_reminder_email(invoice_id,
message, extension_days)`. Wired naively, the auditor denies every call with
"tool not registered". `manifest_gen` derives the YAML from the action side's
real `ToolDefinition`s so they cannot drift — `manifests/collect-reminder.yaml`
is that output, checked in and regenerable.

**3. There was no loop.**
`action-agent-builder/pyproject.toml` declares an
`agent-toolkit = "agent_toolkit.cli:main"` entry point, but no `cli.py`,
runtime or tests exist. `runtime.py` is the gated loop; `cli.py` runs it.

## Design decisions worth knowing

- **Authority comes from a reviewed file, not from the caller.** This is the
  one that matters. `build_integration()` loads `manifests/collect-reminder.yaml`
  by default. If it instead generated a manifest from whatever `task_type` you
  passed, the app would be writing its own permission slip and the auditor
  would grant anything — asking for `collect.pay_suppliers` would simply work.
  Loading a file means an unreviewed task type is refused with *"no policy
  registered"*. Pass `manifest_path=None` (CLI: `--self-declared-manifest`) to
  generate at runtime; it works, warns loudly, and is flagged on the bundle.
- **Read-only tools are not gated.** They execute directly. The audit side
  treats them the same way, so gating them would be theatre.
- **Grants are single-use and narrow.** `allowed_tools` is intersected with
  the runtime's own tools, so the model can only be offered what the auditor
  approved. The bridge re-authorizes per side effect, since a spent grant is a
  replay and the auditor refuses it.
- **A resume executes what the owner approved, nothing else.** It does *not*
  re-ask the auditor: the fact that triggered the hold (a 12500.00 invoice)
  has not changed, so re-adjudicating would just hold again. What changed is
  that a human approved it. `held_action()` is single-use, so a hold cannot be
  resumed twice into two emails.
- **The auditor's reasons go back to the model.** A denial is an observation,
  so the model can adjust instead of failing blind.
- **Fail closed on anything unexpected.** The bridge catches every exception
  from `authorize`/`adjudicate`, not just the auditor's own error types. An
  escaped exception would crash the run and leave the action log without a
  terminal entry, which reads like an action that was attempted and never
  adjudicated.
- **Nothing is edited upstream.** Both packages are used as installed
  dependencies. They were modified recently by other agents and this work must
  not collide with that.

### A gap the auditor cannot currently close

`send_email` takes a `customer_id` *and* an `invoice_id`, and the action side's
handler raises if the invoice does not belong to that customer. The audit rules
have no declarative way to express a two-argument pairing check — `limits` and
`deny_if_fact_true` resolve facts for one subject at a time. So the runtime
surfaces the handler's error as a run `ERROR` rather than as a pre-execution
deny. It fails loudly and safely (nothing is sent), but the refusal happens
after the auditor approved. Closing it properly needs either a pairing fact on
the audit side or a rule shape that can compare two arguments — worth raising
with whoever owns `audit-agent-builder` rather than working around here.

## Layout

```
manifests/
└── collect-reminder.yaml   # the reviewed authority surface; regenerate, don't hand-edit
src/agent_integration/
├── adapters.py       # ToolContractView, HeliosStoreFactSource
├── manifest_gen.py   # audit YAML derived from real tool definitions
├── bridge.py         # authorize → adjudicate → grant lifecycle
├── runtime.py        # gated loop + resume + ScriptedDriver
├── model_driver.py   # GLM-5.3, stdlib HTTP, imported lazily
├── wiring.py         # build_integration()
└── cli.py            # demo / doctor / run / inspect / manifest / selfcheck
tests/
├── conftest.py           # blocks non-loopback sockets; clears model tokens
├── test_integration.py   # 87 tests: adapters, bridge, runtime, evidence
└── test_model_driver.py  # 36 tests: GLM wire protocol against a local mock
```

123 tests. No network, no token — `conftest.py` fails any test that tries to
reach anything off loopback, so the suite cannot quietly depend on a vendor
endpoint or on whose shell it ran in.

Regenerate the manifest after changing any tool:

```bash
agent-integration manifest --out manifests/collect-reminder.yaml
```

Then read the diff. It is the authority surface, so it is worth a human look.

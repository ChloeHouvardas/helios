# The agent loop: what exists, and what to write if you need your own

`specs/architecture-mermaid.md` used to say the runtime provides "the model calls
tools in a loop, built on Qwen-Agent". **The loop exists now, and it is not built
on Qwen-Agent** — no file in this repo imports `qwen_agent` except one lazy
optional import in `audit_agent/advisory.py` behind the `advisory` extra.

Verified 2026-10-15:

- **The loop lives in `platform/agent-integration/`.** `runtime.GatedAgentRuntime`
  authorizes the task, offers the model only what was granted, turns every
  side-effect call into a proposal, adjudicates it through the bridge, then
  executes / holds / denies. Holds resume by id, and exhausting the turn cap is an
  error rather than a silent success.
- **The model driver is `agent_integration.model_driver`.** Qwen over DashScope's
  OpenAI-compatible endpoint, stdlib `urllib` only — no SDK, no dependency tree.
  It returns a proposal and stops, which is what makes the gate unavoidable rather
  than a hook someone could route around.
- **`agent_toolkit` still has no `cli.py`.** Its `pyproject.toml` no longer
  declares the `agent-toolkit = agent_toolkit.cli:main` script entry that used to
  make `pip install -e .` fail; P3 writes the CLI and the entry comes back with it.
- **No token is required for anything offline.** `demo`, `selfcheck`, `inspect`,
  `run --offline` and the whole test suite all work with no key.

So: **you do not have to supply a loop.** Use `build_integration()`. The rest of
this file is for when you want your own — a different model transport, or
deterministic code for an offline demo — which is still a legitimate thing to
want, and still safe because of the seam below.

---

## The seam

A loop is replaceable because it has exactly one job: turn a model's tool requests
into calls on `GatedRuntime`, and turn the results back into messages. The runtime
does not care who produces the requests.

`examples/invoice-payer/app.py` defines the contract. Any loop must use these four
methods and no others:

```python
runtime.tool_specs()                       # -> list of model-facing tool declarations
runtime.read(tool, **kwargs)               # read-only: executes directly
runtime.authorize(summary, invoice_id)     # once per task, before side effects
runtime.propose(tool, arguments, invoice_id=...)   # side effect: gated
```

Plus, for the human:

```python
runtime.pending_holds()
runtime.resolve_hold(hold_id, approve=..., decided_by=...)
runtime.execute_held(proposal)             # only after approve=True
```

`propose()` returns `{"decision": "allow"|"hold"|"deny", "reason": ..., ...}`.
On `allow` it has already executed and included `result`. On `hold` it includes
`hold_id` and `owner_question`. On `deny` it includes only the reason.

**Three rules the loop must obey**, because they are what make the agent
trustworthy rather than merely constrained:

1. **On `hold`, stop.** Surface `owner_question` to the owner. Do not continue the
   task on a different branch.
2. **On `deny`, do not retry with reworded arguments.** Feed the reason back to the
   model as a tool result and let it decide what to fix — or have it report that the
   work cannot be done. Retrying a reworded variant of a denied call is
   circumventing a control, and it is the specific failure a reviewer looks for.
3. **Never invent a value.** The instructions in the action manifest tell the model
   to read before acting; the loop must not paper over a missing read. If the model
   proposes an amount it never looked up, the gate will catch it (that is the point
   of fact-based ceilings) — but a loop that lets it through to execution would not.

---

## Path A: offline / deterministic (what the example does)

Ship this first. It runs with no network, no API key and no dependencies beyond
the three platform packages, which means it demos reliably in a competition room.

`ScriptedPayerAgent` in `examples/invoice-payer/app.py` is a fixed policy that
follows the manifest's instructions literally: look up → check balance → authorize
→ obtain owner-confirmed intent → propose payment → notify. It is not an LLM, and
it does not pretend to be — its docstring says so.

This is legitimate for a prototype **because the thing being demonstrated is the
gate, not the model**. Swap the agent, keep the runtime, and every control still
fires. Say that out loud in the demo; it is the architecture-defence point.

What you lose: the agent cannot handle an unexpected invoice, a partial payment, or
a supplier who asks a question. If the surprise task needs reasoning, you need
Path B.

---

## Path B: a real model loop

**This is implemented.** `platform/agent-integration/` is the real loop, driving
Qwen over DashScope's OpenAI-compatible endpoint with stdlib `urllib` — no SDK, no
`qwen_agent`. Use it rather than writing your own:

```bash
scripts/bootstrap-venv.sh
cp platform/agent-integration/.env.example .env   # set DASHSCOPE_API_KEY
set -a; source .env; set +a

.venv/bin/python -m agent_integration.cli doctor          # is the model reachable?
.venv/bin/python -m agent_integration.cli selfcheck       # do the two packages agree?
.venv/bin/python -m agent_integration.cli run \
    --task "Follow up on overdue invoice INV-1001"
```

`run --offline` uses `ScriptedDriver` instead of the model, so the same command
works with no key — which is what Path A is, reached through the same code path.

### How the implemented loop keeps the gate unavoidable

These are the properties to preserve if you ever write your own, and each is
asserted by a test in `platform/agent-integration/tests/`:

- **The model only ever sees what was granted.** `runtime` narrows the tool list to
  the auditor's grant before calling the driver, so an ungranted tool is not merely
  refused — the model never learns it exists. Asking for an unknown tool comes back
  as text naming the available ones, never as an execution.
- **Tool execution is intercepted structurally, not by convention.**
  `model_driver.next_calls()` returns a *proposal and stops*. It never executes
  anything, so there is no code path by which a model reply reaches `fin_tools`'
  registry directly. An off-the-shelf agent helper that runs the tool loop for you
  would execute side effects before the gate saw them — which is exactly why the
  driver does not do that.
- **One tool call per turn.** Only the first call in a batch is honoured; a batch
  would let the second one execute before the auditor adjudicated it. `GATE_NOTICE`
  tells the model this, because an agent that understands the gate adjusts instead
  of retrying.
- **A `hold` stops the loop.** The run ends with status `held` and a resumable
  `hold_id`; it does not continue while a proposal is queued, because an agent that
  keeps going would double-act. Resume is a separate call and re-authorises nothing
  beyond the exact action that was held.
- **A `deny` returns the reason as a tool result** so the model can adjust. The
  reason is written for a model to act on: it names the field, the problem, and
  what to do instead.
- **Facts come from the fact source, never from the model.** `resolved_facts` the
  agent supplies are used only to *name* subjects worth looking up; their values are
  not believed. The subject id must be one of the fixed keys — `invoice_id`,
  `bill_id`, `customer_id`, `supplier_id`, `subject_id`, `payment_id` — or facts
  will not resolve and every fact-bound rule will deny.
- **A broken model is an error, never an allow.** A driver exception ends the run as
  `error`. So does exhausting `max_turns`, which would otherwise look like success.

`specs/build1.md` step 2 describes building a plain loop first and adding
interception at step 3. That ordering is still the right way to build one from
scratch, but you no longer have to: the gate is already interposed, and
`selfcheck` is the CI guard that the two packages still agree about tool names,
side-effect flags and schemas.

### If you need a different model transport

Subclass or replace `model_driver.ModelDriver`; it is the only module that needs a
token and the only one that imports an HTTP client. Keep the `Driver` protocol
(`next_calls(*, task, instructions, tools, history)` returning one `ToolCall` list
or a final string) and everything above still holds. Competition rule: Qwen is the
only generative model allowed, so a different *transport* is fine and a different
*provider* is not.

---

## Which to build

Neither — **both already exist**, and they are the same code path. `run --offline`
gives you Path A (deterministic, no key, cannot fail on stage) and `run` gives you
Path B (real Qwen, needs a key). `build_integration()` is underneath both.

So the choice is now about **what you demo**, not what you write:

- **Demo Path A when the gate is what is being judged.** It needs no key, no
  network and no quota, and it cannot fail in front of judges. Say out loud that
  the agent is scripted and that swapping it for a model changes no verdict — that
  is the architecture-defence point.
- **Demo Path B when the model's reasoning is what is being judged**, or when the
  surprise task needs an agent that can handle an unexpected invoice, a partial
  payment, or a supplier who asks a question. Run `doctor` first; it makes one
  minimal request and tells you exactly which setting is wrong.

Keep `--offline` reachable in the demo either way. If the key or the network fails
on stage, falling back to the scripted run takes one flag and shows the same
controls firing — which is the thing actually worth proving.

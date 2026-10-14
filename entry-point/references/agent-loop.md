# The agent loop: what to write, and what already exists

`specs/architecture-mermaid.md` says the Agent Toolkit runtime provides "the model
calls tools in a loop, built on Qwen-Agent". **It does not yet.** This file says
what is missing, what you must therefore write, and how to do it without breaking
the gate.

Verified 2026-10-14:

- `agent_toolkit` has **no loop and no CLI**, although its `pyproject.toml`
  declares an `agent-toolkit = agent_toolkit.cli:main` script entry point. That
  entry point will fail as written — there is no `cli.py`.
- `qwen_agent` is declared as a dependency (`>=0.0.17`, and `0.0.34` appears in
  `platform/action-agent-builder/uv.lock`) but **is not installed**.
- No `DASHSCOPE_API_KEY` is configured.
- No file in this repo imports `qwen_agent`. There is no working example to copy.

So: **you supply the loop.** Everything below the loop — interception, gating,
logging, holds — exists and works. That is the useful part, and it is the part
that is hard to get right.

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

Two sub-paths, and which one you can use depends on whether `qwen_agent` gets
installed and a key configured. **Do not guess its API** — no file in this repo
uses it, so there is nothing here to copy, and its surface differs between
versions.

### B1. With Qwen-Agent (intended design)

Once `qwen_agent` is importable:

```bash
python3 -m pip install 'qwen-agent>=0.0.17'   # or use the package's uv.lock
export DASHSCOPE_API_KEY=...                    # from the environment, never in code
python3 -c "import qwen_agent; print(qwen_agent.__version__)"
```

Then, and only then, read the installed version's own docs/source to learn the
current function-calling agent class and how it accepts tool declarations. Wire it
so that:

- `runtime.tool_specs()` is what you hand the model. Note that these are shaped for
  Qwen-Agent function calling — `agent_toolkit.ToolDefinition.to_spec()` produces
  `{"name", "description", "parameters"}` for exactly this reason.
- **Tool execution is intercepted.** The model must never reach `fin_tools`'
  registry directly. Every call goes through `read()` or `propose()` depending on
  the tool's side-effect flag. This is the one place where a shortcut silently
  destroys the platform's central claim, so it is worth a test of its own.
- A `hold` returns to the model as "the action was held pending the owner's
  decision" and ends the turn. A `deny` returns the reason as a tool result so the
  model can adjust.
- `max_turns` from the action manifest bounds the loop.

`specs/build1.md` step 2 describes this as "a Qwen-Agent agent calls both tools end
to end, with no safety layer yet", then adds interception at step 3. That ordering
is still the right way to build it: get a plain loop working, then interpose the
gate, and confirm the same scenarios still produce the same verdicts.

### B2. Without Qwen-Agent (any model, direct tool calling)

If the dependency cannot be installed, write the loop yourself against whatever
tool-calling API you have. It is ~60 lines and has no Helios-specific subtlety:

```
messages = [system = manifest.instructions]
tools    = runtime.tool_specs()
runtime.authorize(task_summary, subject_id)
for turn in range(manifest.max_turns):
    response = model(messages, tools)
    if response has no tool calls: return response text
    for call in response.tool_calls:
        if not side_effecting(call.name):
            result = runtime.read(call.name, **call.arguments)
        else:
            outcome = runtime.propose(call.name, call.arguments,
                                      invoice_id=subject_of(call.arguments))
            if outcome["decision"] == "hold":
                → surface outcome["owner_question"] to the approval queue
                → stop the turn; resume only after resolve_hold()
            elif outcome["decision"] == "deny":
                result = {"error": outcome["reason"]}      # fed back, not retried
            else:
                result = outcome["result"]
        messages.append(tool_result(call.id, result))
```

The only non-obvious parts: **`hold` must actually stop the loop** (an agent that
keeps going while a proposal is queued will double-act), and **the subject id must
be extracted from the arguments** so fact resolution can find the record. Use one
of the fixed subject keys — `invoice_id`, `bill_id`, `customer_id`, `supplier_id`,
`subject_id`, `payment_id` — or the facts will not resolve and every fact-bound
rule will deny.

---

## Which to build

For an 80-hour competition with a surprise task: **ship Path A, and build Path B
only if the loop is genuinely what is being judged.**

The reason is asymmetric. Path A costs an hour and cannot fail on stage. Path B
costs most of a day, depends on a package that is not installed, and if it fails it
fails in front of judges. The gate — the thing that is actually novel here — is
identical in both.

If you do build Path B, keep Path A behind a flag so the demo can fall back.

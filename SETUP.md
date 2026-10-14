# Setup

## 1. Build the environment

From the repo root:

```bash
scripts/bootstrap-venv.sh
```

This creates `.venv` on Python 3.13, installs the four first-party packages
editable (`agent-toolkit`, `audit-agent-builder`, `agent-integration`,
`fin-tools`) and the pinned dependencies in `requirements-dev.txt`.

Python 3.13 is required: `audit_agent` and `agent_integration` both declare
`requires-python = ">=3.13"`. Use `.venv/bin/python` for everything — the
system `python3` is not guaranteed to be 3.13 and has no dependencies
installed.

## 2. Run the tests

```bash
scripts/test-all.sh
```

Every suite runs offline. No network, Docker daemon, model token or provider
credential is needed. Expect one `== <suite>: N passed` line per suite and
`all N suites green` at the end.

## 3. Set a model key (only for live agent runs)

Copy `platform/agent-integration/.env.example` to `.env` and set a DashScope
API key. The model driver reads, in priority order:

1. `HELIOS_LLM_API_KEY`
2. `DASHSCOPE_API_KEY`
3. `OPENAI_API_KEY`

Never commit a `.env`; it is gitignored. Never print or log a key.

Check the wiring without spending quota:

```bash
.venv/bin/python -m agent_integration.cli doctor
```

It prints `OK` when a key is present and a clear "no key" message otherwise.
The whole platform is testable with no key at all — the audit agent is
deterministic and the runtime has a `ScriptedDriver` for offline runs.

## 4. Where to go next

- `HANDOFF.md` — what exists, what works, and the traps that cost hours
- `AGENTS.md` — commit conventions and scope discipline
- `specs/decisions.md` — the decisions already made, and which are still open
- `QWEN.md` — the short brief every agent session loads first

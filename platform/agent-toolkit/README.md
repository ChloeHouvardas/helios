# Agent Toolkit

The shared agent runtime packaged into generated applications: agent loop, action interception, audit gate, action log, and holds.

Built on Qwen-Agent. Design principle: **the model proposes, code decides** — agents cannot exceed their mandate, because the code that executes actions enforces it.

App-specific agent behavior is data (an agent manifest), not code. See `specs/architecture-mermaid.md` and `specs/decisions.md`.

Build 1 scope: `dev-resources/build1.md`.

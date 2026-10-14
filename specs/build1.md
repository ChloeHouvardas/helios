# Build 1: Agent Toolkit Runtime

This document defines what is being built in Build 1. The overall architecture lives in `architecture-mermaid.md`, `decisions.md`, `interfaces.md`, and `tech-stack.md`; this document only scopes the first build.

## Goal

An agent built with the Agent Toolkit can use tools, and every action with a side effect becomes a proposal that is approved, held, or rejected against the agent's mandate, with every step recorded.

## Build order

1. **Tool contract and mock tools:** define the tool contract (name, input schema, output schema, side-effect flag), then build one read-only and one side-effect mock tool.
2. **Plain agent loop:** a Qwen-Agent agent calls both tools end to end, with no safety layer yet.
3. **Interception:** side-effect calls become proposals instead of executing.
4. **Audit gate:** a mandate check plus a few deterministic rules that return approve, hold, or reject.
5. **Action log and holds:** an append-only log, then pausing on a hold and resuming once the owner decides.
6. **Agent manifest:** the format Qwen fills in to create a new agent from data alone, with validation and a short guide.

## Reference agent: Collect Reminder

Build 1 is developed and tested against one agent.

- **Job:** follow up with a customer about an overdue invoice. Send a reminder, and offer a due-date extension if the customer asks for more time.
- **Tools:** one read-only tool to look up the invoice and customer history, one side-effect tool to send an email (which can include an extension offer).
- **Mandate:** maximum extension length, owner approval for longer extensions, a limit on how often a customer is contacted, and owner approval for large invoices.

## Done when

- A normal reminder is approved and executes.
- A larger extension request is held, and the agent resumes after the owner decides.
- A request outside the mandate is rejected, and the agent adjusts.
- Every step appears in the action log.
- The Collect Reminder agent runs from its manifest.

## Not in Build 1

These are part of the overall architecture but have been left out of this build:

- **Real financial tools:** mock tools that follow the tool contract are used instead.
- **LLM-based audit checks:** the audit gate uses deterministic rules only.
- **Post-hoc audit review:** reviewing the action log for patterns over time.
- **Triggers and scheduling:** agents are run manually.
- **Approval UI:** owner decisions are made through a simple interface or CLI.
- **Other apps:** only the Collect Reminder agent, plus one simple test agent for the manifest.
- **Shared services integration:** no connection yet to the knowledge graph, Intent & Trust Service, or shared events.

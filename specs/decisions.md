# Architecture Decisions

This document tracks current architectural decisions and unresolved questions.

## Decided

### Applications run independently
Generated applications contain their own UI, business logic, action agents, and audit agents.

Agents execute within their deployed applications rather than in a centralized Helios agent runtime.

### Agent builders are frameworks
The Action Agent Builder and Audit Agent Builder are reusable development toolkits used by Qwen to create agents.

The toolkit includes a shared agent runtime (agent loop, action interception, audit gate, logging, holds) that is packaged into each generated application. Helios maintains the runtime code; each application runs its own copy.

### Actions are proposals, decided by code
Action agents never perform side effects directly. Any tool call with a side effect becomes a **proposal**, which the audit gate approves, holds for the owner, or rejects.

Read-only tool calls run directly.

### Audit decisions are deterministic
The audit gate is plain, testable code checked against the app's **mandate**, not an LLM. It fails closed: unknown tools, malformed arguments, or unrecognized constraints are not allowed through.

### Agents operate under a mandate
Each agent has a mandate defining what it may do: allowed tools, limits (e.g. maximum extension length, contact frequency), and which actions need owner approval.

### Tools declare side effects
Every tool follows a shared contract: name, input schema, output schema, and a side-effect flag. The side-effect flag determines whether a call is gated.

### App-specific agent behavior is data
What differs between agents (instructions, tool list, mandate) is defined as data in an agent manifest, separate from the runtime. Qwen generates the manifest rather than writing agent logic from scratch.

### Agent loop built on Qwen-Agent
The agent runtime builds on Qwen-Agent's function-calling agent, with interception added at the tool-execution step.

### Knowledge graph is shared
Helios hosts a central knowledge graph that applications can use to exchange relevant state and context.

### Qwen handles application generation
Qwen uses Helios's development interfaces to generate applications and initiate deployment.

## Tentative

### Hybrid financial tools
Some financial capabilities may be packaged into applications, while others are provided through centrally managed APIs.

Managed APIs are preferred for operations that benefit from centralized maintenance, security, or regulatory updates.

### Shared trust services
Helios may centrally enforce sensitive financial permissions and approval requirements, while app-specific audit agents run locally.

The local audit gate checks each action against the app's mandate. Sensitive operations may additionally require a check by Helios's Intent & Trust Service.

### Shared ledger
A shared ledger may store verified financial events and records used across applications.

### Append-only action log
Every tool call, proposal, decision, and result is recorded in an append-only log. Hash-chaining entries for tamper evidence is a likely addition.

### Holds and resumption
When an action is held, the agent's state (conversation history and the proposal) is saved. Once the owner decides, the agent resumes from where it stopped. An approval applies only to the exact action that was held.

## Open Questions

- Where will generated applications be hosted?
- What is the deployment artifact format?
- Where is held-agent state stored (in the app, or a shared Helios service)?
- How will the knowledge graph distinguish authoritative records from agent observations?
- How will applications subscribe to cross-app updates?
- Which financial operations should be managed APIs versus packaged SDK tools?
- How will applications authenticate with shared Helios services?
- What platform-wide limits should mandates be capped by?

## Updating Decisions

When a design choice is resolved, move it to **Decided** and update the relevant architecture or interface documentation.

# Component Interfaces

This document outlines how Qwen, generated applications, and Helios services interact.

Detailed API contracts will be defined as implementation progresses.

## 1. Qwen ↔ Helios Development Platform

**Purpose:** Allow Qwen to discover platform capabilities and build applications.

Expected capabilities:
- Discover financial tools through MCP and SDK interfaces.
- Create applications using Helios app specifications.
- Build action agents using the Action Agent Builder, by writing an agent manifest.
- Build audit agents using the Audit Agent Builder.
- Submit applications for validation and deployment.
- Receive feedback and revise generated applications.

## 2. Generated Apps ↔ Helios Shared Services

**Purpose:** Allow independently deployed applications to access shared capabilities.

How it works when deployed:
- **Tools.** The app proposes a named tool (`payments.send`, `graph.publish_verified`) through `helios_sdk.propose_action` to its audit sidecar (`POST /execute`, bearer `HELIOS_ACTION_TOKEN`). The sidecar enforces the fact-free rules and the permission families from `helios.yaml`, then forwards granted tools to the tenant core's `POST /call {"tool", "args"}` with the execution secret. Three outcomes come back and stay distinct: `executed`, `denied` (the sidecar or core's own gate refused this request), or 502 `retryable` (an outage, never a decision).
- **Graph.** `graph.publish_verified` / `graph.observe` / `graph.read` are tools like any other; verified nodes persist in the core's SQLite snapshot.
- **Events.** `helios_sdk.publish_event(name, **data)` and `subscribe_events(handler)` on the tenant Redis channel `<tenant>:events`. Event names are `<entity>.<event>` from the app spec's `graph.publishes`.
- **Authorization.** A payment needs an owner-confirmed intent `auth_code`; core rejects a send without one whatever the bearer token says.
- **Permissions.** `helios.yaml` `permissions` lists the families the sidecar may forward: `payments`, `email`, `knowledge-graph`, `events`, `forecast`, `market-data`, `spreadsheets`, `collect`. An empty list grants nothing.

Locally and in tests the same registry is composed in-process by `fin_tools.build_world()`.

## 3. Action Agents ↔ Audit Agents

**Purpose:** Support app-specific workflow execution and monitoring.

- Both run within their generated application, using the Agent Toolkit runtime.
- Action agents perform tasks and request actions through financial tools.
- Read-only tool calls run directly.
- Tool calls with side effects become **proposals** and are not executed until reviewed.
- The audit gate reviews each proposal against the agent's **mandate** and returns a **verdict**: approve, hold, or reject, with a reason.
  - **Approve:** the action executes.
  - **Hold:** the agent pauses until the owner decides, then resumes.
  - **Reject:** the reason is returned to the action agent so it can adjust.
- Every tool call, proposal, verdict, and result is written to an append-only action log.
- Sensitive actions may additionally require checks by Helios's shared trust services.

Shapes in use (exact schemas to be finalized during implementation):
- **Proposal:** which agent, which tool, the arguments, and a unique ID.
- **Verdict:** decision, reason, and which rule produced it.
- **Mandate:** allowed tools, limits, and which actions need owner approval.

## 4. Applications ↔ Shared Knowledge Graph

**Purpose:** Maintain context across applications.

Example:
1. A customer payment app confirms a payment.
2. The app publishes the updated payment state.
3. Helios updates the shared state and related graph information.
4. An invoice app retrieves or receives the update.
5. Its agent adjusts the invoice-follow-up workflow.

The distinction between verified records and agent-generated observations will need to be defined.

## 5. Deployment Interface

**Purpose:** Allow Qwen to deploy generated applications.

Flow:

Generate → `helios-app validate` → fix every problem at once → `helios deploy <dir> --tenant <t>` → `GET /health` 200 at `http://localhost/apps/<app>-<tenant>/`.

An app provides `helios.yaml`, an ASGI entry and a constant `GET /health`. The engine (`platform/deployment`) validates the manifest, lints for secrets, builds on the golden base image, smoke-tests, attaches the audit sidecar and routes through nginx. CLI exit codes are for the coding agent: `0` deployed, `1` rejected (the response lists every problem and the build or smoke log), `2` refused before upload (a `.env` in the bundle), `3` engine unreachable, `4` usage. A failed redeploy leaves the previous deployment serving.

Operator surface: `helios apps`, `helios logs <container>`, `helios evidence --tenant <t>` (every app's action, audit and sidecar logs with chain verification), `helios undeploy <name>` (containers removed, data volumes kept), `helios doctor`.

## 6. Tool Contract

**Purpose:** Give every financial tool a consistent description, so agents and the audit gate can treat tools uniformly.

Each tool declares:
- Name and description
- Input schema
- Output schema
- Side-effect flag: whether calling it changes anything in the real world

The side-effect flag determines whether a call runs directly or goes through the audit gate. Any implementation that follows the contract, including mocks, can be swapped in without changing the agent runtime.

### Call outcomes

A tool call has three distinguishable outcomes, and conflating the last two corrupts the action log:

| Outcome | Meaning | What the agent should do |
|---|---|---|
| `ok: true` | the tool ran and returned a result | use the result |
| `rejected: true` | a gate or validator looked at this request and refused it | change the request, or report that it cannot be done |
| `unavailable: true` | nobody decided anything; the answer could not be obtained (no credential, timeout, upstream refusal) | the request may be fine — retry, or report the outage |

An outage must never be reported as a rejection. Recording one as a rejection writes a gate decision into the audit log that no gate ever made, and sends the agent "fixing" a call that was never wrong.

### Market data tools

`market.*` tools are read-only quote-data reads and declare `side_effects=False`. They carry no credential in their declaration, accept no endpoint name from the caller (the endpoint is hard-coded per tool against a closed allow-list), and return floats with an explicit unit and `as_of` date. Quote data never reaches a balance or the ledger — see `specs/decisions.md`.

## Interfaces to Define Later

- Audit agent definitions beyond the deterministic gate (advisory review is non-binding today)
- A tenant-wide read of the knowledge graph for apps (today apps read core's graph only through the sidecar; cross-app reads go over the bus)
- Holds and replay guards persisted across a container restart

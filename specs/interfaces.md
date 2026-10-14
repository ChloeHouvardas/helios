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

Expected capabilities:
- Read and publish shared business state.
- Query the cross-app knowledge graph.
- Access managed financial tools.
- Receive relevant updates from other applications.
- Request authorization for sensitive operations.

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

Expected flow:

Generate → Validate → Return Feedback → Revise if Needed → Deploy → Verify

## 6. Tool Contract

**Purpose:** Give every financial tool a consistent description, so agents and the audit gate can treat tools uniformly.

Each tool declares:
- Name and description
- Input schema
- Output schema
- Side-effect flag: whether calling it changes anything in the real world

The side-effect flag determines whether a call runs directly or goes through the audit gate. Any implementation that follows the contract, including mocks, can be swapped in without changing the agent runtime.

## Interfaces to Define Later

- Generated application package format — spec format defined in `specs/app-builder.md`; packaging and hosting still open
- Agent manifest format
- Audit agent definitions beyond the deterministic gate
- Financial tool schemas and permissions (contract shape agreed; individual tool prototypes live in `platform/financial-tools/`, see its README and `research/README.md`)
- Knowledge graph read/write interface
- Shared event format
- Deployment API and hosting model

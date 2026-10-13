# Component Interfaces

This document outlines how Qwen, generated applications, and Helios services interact.

Detailed API contracts will be defined as implementation progresses.

## 1. Qwen ↔ Helios Development Platform

**Purpose:** Allow Qwen to discover platform capabilities and build applications.

Expected capabilities:
- Discover financial tools through MCP and SDK interfaces.
- Create applications using Helios app specifications.
- Build action agents using the Action Agent Builder.
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

- Action agents perform tasks and interact with financial tools.
- Audit agents review actions, outcomes, and alignment with user intent.
- Both run within their generated application.
- Sensitive actions may additionally require checks by Helios's shared trust services.

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

## Interfaces to Define Later

- Generated application package format
- Action and audit agent definitions
- Financial tool schemas and permissions
- Knowledge graph read/write interface
- Shared event format
- Deployment API and hosting model
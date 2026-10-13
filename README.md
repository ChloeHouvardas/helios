# Helios

Helios is a platform for generating and deploying agentic financial applications using Qwen.

The platform provides reusable financial tools, application-building frameworks, and infrastructure that generated applications can use.

## Architecture

Helios has three main parts:

1. **Development Platform** — Tools and frameworks Qwen uses to build applications, action agents, and audit agents.
2. **Generated Applications** — Independently deployed applications containing their own UI, business logic, and agents.
3. **Shared Services** — Centrally managed financial APIs, cross-app knowledge graph, shared state, and trust services.

## Goals

- Enable Qwen to build and deploy financial applications with minimal manual intervention.
- Provide reusable tools rather than rebuilding financial capabilities for every app.
- Support action agents and audit agents within generated applications.
- Allow applications to share relevant business state through a central knowledge graph.
- Support secure financial operations and user-defined constraints.

## Documentation

- [Architecture](specs/architecture-mermaid.md)
- [Interfaces](specs/interfaces.md)
- [Decisions](specs/decisions.md)
- [Tech Stack](specs/tech-stack.md)

The architecture is evolving. Implementation details can be refined as components are developed.
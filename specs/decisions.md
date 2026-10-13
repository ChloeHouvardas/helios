# Architecture Decisions

This document tracks current architectural decisions and unresolved questions.

## Decided

### Applications run independently
Generated applications contain their own UI, business logic, action agents, and audit agents.

Agents execute within their deployed applications rather than in a centralized Helios agent runtime.

### Agent builders are frameworks
The Action Agent Builder and Audit Agent Builder are reusable development toolkits used by Qwen to create agents.

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

### Shared ledger
A shared ledger may store verified financial events and records used across applications.

## Open Questions

- Where will generated applications be hosted?
- What is the deployment artifact format?
- How will agents persist state and resume long-running tasks?
- How will the knowledge graph distinguish authoritative records from agent observations?
- How will applications subscribe to cross-app updates?
- Which financial operations should be managed APIs versus packaged SDK tools?
- How will applications authenticate with shared Helios services?

## Updating Decisions

When a design choice is resolved, move it to **Decided** and update the relevant architecture or interface documentation.
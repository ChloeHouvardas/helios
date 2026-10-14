
# Platform Capability Proposal | 平台能力方案

**Team / 团队:** Maple Queen’s

**Platform / 平台:** Helios

## 1. Platform Vision | 平台愿景

Our platform makes it possible for anyone, especially small-business owners, to create custom financial applications with AI.

Today, a regular coding agent can generate code, but it cannot easily build reliable fintech products on its own. Financial applications need more than UI generation; they need financial workflows, data handling, application hosting, and checks for correctness and safety.

Our platform gives Qwen the tools to build complete small-business financial applications such as storefronts, invoicing tools, payment tools, and financial management apps. It combines reusable financial infrastructure with AI financial intelligence in one platform.

One of our key features is an auditing loop agent. After Qwen generates an application, the auditing loop reviews the app for functionality, financial correctness, security, and user flow issues, then tells Qwen what to fix. This makes it possible to produce high-quality finance apps than a normal coding agent could not build alone.

## 2. User Experience | 用户体验

A user starts with a financial idea in natural language.

For example, a small-business owner might ask for an invoicing app, a storefront with payments, or a business finance management tool.

Qwen connects to our platform through the MCP server, reads the platform documentation and capabilities, and generates a compatible application. The application can include frontend pages, workflows, and financial features.

The auditing loop then reviews the generated app, identifies issues, and sends improvement instructions back to Qwen. After revision, the app is deployed through the platform and becomes available to the user.

Once deployed, the user can open and use the application through the platform. New applications follow the same process, allowing the platform to keep producing more financial apps over time.

## 3. Platform Capabilities | 平台能力

### 3.1 AI App Creation Interface

The platform provides an MCP server, clear documentation, reusable APIs, and development patterns that allow Qwen to generate platform-compatible financial applications.

### 3.2 App Deployment and Hosting

The platform supports application registration, deployment, and operation so generated apps can run through the platform and be used by end users.

### 3.3 Financial Toolkit

The platform provides reusable financial capabilities for common small-business use cases such as payments, invoicing, transaction handling, and financial management.

### 3.4 AI Financial Intelligence

The platform provides AI-powered financial understanding that helps applications work with financial information, workflows, and decisions more effectively.

### 3.5 Auditing Loop Agents

The platform includes an auditing loop agent that reviews generated applications for functionality, financial correctness, security, and user experience issues, then provides feedback for revision. The auditing process happens in development and during deployment. The agent will monitor and correct deployed agentic workflows.

### 3.6 Agentic Workflow Support

The platform supports agentic workflows so applications can include multistep financial tasks and AI-assisted operations.

### 3.7 Shared Platform Services

Applications can use shared platform services for data access, application logic, and financial operations, making it easier to build new apps without recreating core infrastructure each time.

## 4. Development Commitment | 开发承诺

| ID | Committed Capability / 承诺交付的平台功能 |
|---|---|
| C01 | Application generation interface: Users can provide a financial app idea, and Qwen can use the platform’s MCP server, documentation, and APIs to generate a compatible application. |
| C02 | Application deployment and operation: Generated applications can be registered, deployed, run, and accessed through the platform. |
| C03 | Financial toolkit: The platform provides reusable financial capabilities that support small-business application creation, including core finance-related operations needed by the three flagship apps. |
| C04 | AI financial intelligence: Applications can call platform capabilities that help interpret financial information and support finance-related workflows. |
| C05 | Auditing loop review: The platform includes an auditing loop agent that reviews generated apps for functionality, financial correctness, security, and user flow quality, then returns actionable feedback. |
| C06 | Revision loop with Agents: Qwen can revise an application based on feedback from the auditing loop so the app improves before final deployment. |
| C07 | Agentic workflow support: Generated applications can include AI-driven workflows built on top of the platform’s shared capabilities which are monitored by agents. |
| C08 | Reusable app foundation: Multiple distinct financial applications can be created on the same platform using shared infrastructure and a common integration pattern. |

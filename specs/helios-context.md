# Helios: Context Brief

This document describes the Helios platform, the competition it is being built for, and its three flagship apps. It is intended as background context only.

---

## 1. The Competition

**Event:** Chengdu 80 2026 Fintech Hackathon, an 80-hour competition.
**Team:** Maple Queen's
**Platform:** Helios

### The challenge

Build an AI-native financial application platform that lets users turn financial ideas into working, reusable applications, and keeps producing new ones.

### Deliverables

- **Platform:** a working host platform that packages financial functions, tools and data as reusable APIs with clear developer documentation. Given a user's idea plus the platform's docs and APIs, an LLM (Qwen) must be able to generate a compatible, plugin-like app that deploys and runs on the platform.
- **Three flagship apps:** three clearly different financial apps, each built for the platform and deployed and run through it, usable by ordinary users or developers. Stand-alone programs without real platform integration do not qualify.
- **Surprise task:** after the code lock, judges give a new financial app requirement in natural language. It may involve financial data or other capabilities available through the platform. Engineers check whether a new AI-built app can be made compatible, deployed and used through the platform.

### Rules

- Qwen is the only generative model allowed for development.
- Documentation and interfaces should not depend on hidden prompts or model-specific tricks.
- No hard-coded API keys; credentials must be kept out of logs and packages.
- Data comes from the organizers (including Tushare, which covers Chinese market data, company financials and macro data) and legally available public data. Judges score what the platform does with data, not data exclusivity.

### Scoring

| Stage | Weight | Focus |
|---|---|---|
| Engineering verification | 25% | System runs, platform completeness, app delivery through the platform, surprise task |
| Architecture and technical defense | 25% | Architecture, extensibility, security, usability, engineering design |
| Investment pitch | 50% | Market opportunity, product innovation, value of the three flagship apps, business model and growth, investment attractiveness |

**The judges' guiding question:** what new financial capability becomes possible when AI, data and an extensible platform work together?

---

## 2. What Helios Is

Helios is a platform that lets small businesses get custom financial apps built for their own workflow, with AI agents inside those apps that carry out financial work for them.

**The problem:** small businesses spend significant time and money on ongoing financial work, such as collecting payments, paying suppliers and managing cash, often across tools that do not connect.

**Who it is for:** small and medium businesses, and the owners, finance staff and bookkeepers who handle their finances.

---

## 3. How It Works

1. A user describes the app they want, for example: "Build an app that manages my invoices, using Helios."
2. Qwen, acting as a coding agent, builds the app using the Helios platform's tools, SDK and documentation.
3. The app is audited, with feedback going back to Qwen until it is ready.
4. The app is deployed automatically onto the platform.
5. Inside the deployed app, AI agents carry out the financial work the app was built for.

There are two kinds of AI involved:

- **The coding agent (Qwen):** builds apps.
- **Agents inside apps:** do financial work once an app is running.

---

## 4. Architecture

Helios has two main layers.

### Layer 1: App Creation Platform

The tools and building blocks Qwen uses to create apps, and the agents that run inside them.

- **Financial Tools Developer Interface:** the financial capabilities apps can use. These include:
  - Populating the knowledge graph (the shared record of a business's financial activity)
  - Sending email
  - Spreadsheet tools
  - Payments: sending and receiving money
  - Simulating outcomes
  - Intent verification
- **App Builder SDK:** the kit that helps Qwen set up an app on Helios. It covers knowledge graph setup, a database schema tool, basic UI guidelines, tech stack definitions, and a question phase about the existing architecture.
- **Action Agent:** the component concerned with how agents inside apps decide which actions to take.
- **Audit Agent:** the component that audits agent activity.

### Layer 2: App Lifecycle and Deployment Infrastructure

The machinery that turns generated code into running apps.

- **Build & Audit Agent Engine:** builds apps and audits them, sending feedback to the coding agent.
- **Automatic Deployment Engine:** deploys finished apps onto the platform.

---

## 5. The Three Flagship Apps

### Helios Collect: AI Receivables Agent

**Problem:** small businesses often wait weeks or months to be paid for work they have already delivered, and chasing those payments takes ongoing effort that is easy to put off.

**What it does:** tries to collect payments from customers. If a payment has not been made, it negotiates with the customer on reasonable terms, such as extending the due date. When payment arrives, the money is directed to the right place.

### Helios Payables: AI Payables Agent

**Problem:** paying suppliers is largely manual. Due dates get missed, money is not always in the right account when a bill is due, and rushed payments risk duplicates or fraud.

**What it does:** collects supplier invoices, tracks due dates, makes sure funds are available, and pays bills on time. Duplicate invoices, changed supplier payment details and unusual amounts are held for the user to verify.

### Helios Cash Planner: AI Cash Flow Agent

**Problem:** small business owners often discover a cash shortage only when payroll or a major bill is already due, because typical forecasts assume customers pay on time.

**What it does:** forecasts cash for the weeks ahead based on how customers actually pay, warns the user early about shortfalls, recommends actions such as which customers to follow up with or which payments to reschedule, and lets users explore what-if scenarios in plain language. It uses public financial market data, such as borrowing costs, to help users compare options.

# Research: Intent Verification & Simulating Outcomes

How the two hardest features in this package are grounded in current research.
Every design choice below maps to a cited source. Retrieved 2026-10-14 via web search.

---

## 1. Intent verification

### 1.1 The problem, stated precisely
An LLM action agent can execute something the user never meant: a wrong payee, an
inflated amount, a payment "authorized" by text the agent itself generated.
Two 2026 papers name the property we must enforce:

- **Intent-to-Execution Integrity** — *Securing LLM Agents Need Intent-to-Execution
  Integrity* (arXiv:2605.16976, May 2026). Argues agent execution must *faithfully
  reflect* a captured user intent, and that today's agents lack any mechanism to bind
  the two. <https://arxiv.org/html/2605.16976v1>
- **Intent-verified delegation chains** — *SentinelAgent: Intent-Verified Delegation
  Chains for Securing Federal Multi-Agent AI Systems* (arXiv:2604.02767, Apr 2026).
  Defines a verification lifecycle where intent is verified **before execution**
  (property P2) and each delegated step carries a verifiable authorization
  ("Agentic JWT" binds agent actions to user intent). <https://arxiv.org/abs/2604.02767>

Both converge on the same architecture: capture intent as a **signed, immutable
artifact**, then make every side-effecting call prove it matches that artifact.

### 1.2 The financial-regulation anchor: PSD2 dynamic linking
Payments already solved a version of this problem legally. PSD2 Art. 97(2) + the
Commission Delegated Regulation (RTS on SCA/CSC) require that for remote payments the
authentication code be **dynamically linked** to a specific amount and a specific
payee, and that *any change to amount or payee invalidates the code*:

- EBA Single Rulebook Q&A **2020_5133** ("Dynamic linking: transactions for which the
  final amount is not known") — confirms Art. 97(2) requires SCA elements that
  dynamically link the transaction. <https://www.eba.europa.eu/single-rule-book-qa/qna/view/publicId/2020_5133>
- EBA Q&A **2020_5366** — clarifies where the authentication code is created and that
  it links amount + payee. <https://www.eba.europa.eu/single-rule-book-qa/qna/view/publicId/2020_5366>
- EBA consultation response: "Any change to the amount or payee shall result in an
  invalidation of the authentication code." <https://www.eba.europa.eu/eba-response/100>
- Practitioner explainer of the three-part dynamic-linking requirement: OneSpan,
  *PSD2: How to Perform Dynamic Linking in a Compliant, Convenient Way*.
  <https://www.onespan.com/blog/psd2-how-perform-dynamic-linking-compliant-convenient-way>

**What we copied:** `fin_tools.intent_verification` issues an HMAC auth code over
`(intent_id, amount_cents, currency, payee, nonce)`. Changing any field fails
verification. Codes are single-use and expire. This is PSD2's rule applied to
*agent* payments: the human's confirmation is the SCA moment; the agent cannot
re-synthesize it.

### 1.3 Why not just ask the LLM to double-check?
Runtime-verification literature shows probabilistic checks alone are not accepted as
guarantees, and that deterministic/formal monitors are the layer that carries
guarantees while LLMs translate intent into checkable form:

- *VeriGuard: Enhancing LLM Agent Safety via Verified Code Generation*
  (arXiv:2510.05156, 2025) — translate user intent + spec into verified guards.
  <https://arxiv.org/abs/2510.05156>
- *AgentGuard: Runtime Verification of AI Agents* (arXiv:2509.23864, 2025) — formal
  automata constrain high-level agent behaviour at runtime.
  <https://arxiv.org/html/2509.23864v1>
- Survey: *Toward Safe LLM Agents: A Survey of Specification, Verification, and
  Enforcement* (arXiv:2608.14590, 2026). <https://arxiv.org/pdf/2608.14590>
- *RefineAct: Automatic Runtime Verification of LLM Agent Actions* (ACM, 2026) —
  runtime refinement-gap checking for agent actions.
  <https://dl.acm.org/doi/abs/10.1145/3832783.3834409>

**Our split (matches Helios house doctrine):** deterministic HMAC + state machine =
the guarantee; an optional LLM hook (`intent_assessor`) may *propose* a natural-language
intent summary to show the human at confirmation time, but never decides. Probabilistic
proposes, deterministic disposes.

---

## 2. Simulating outcomes

### 2.1 Emulate the tool call before running it: ToolEmu
- *Identifying the Risks of LM Agents with an LM-Emulated Sandbox* (Ruan et al.,
  **ICLR 2024 Spotlight**, arXiv:2309.15817). Emulates tool execution from specs +
  inputs alone, no real environment; human evaluation found **68.8%** of failures it
  identified were valid real-world risks, and even the safest agent failed 23.9% of
  scenarios. <https://arxiv.org/abs/2309.15817>, repo
  <https://github.com/ryoungj/toolemu>
- Still the reference point in 2026 surveys/benchmarks (e.g. *AgentS4D*,
  arXiv:2607.27294; *SkillSentry*, arXiv:2608.03485) as "the LM-emulated sandbox".

**What we copied:** `fin_tools.simulation.sandbox_run` executes a proposed tool call
against a **deep copy** of world state and returns the would-be effects + resulting
ledger/outbox deltas, without touching the real state. Ours is deterministic
copy-on-write rather than LM-emulated (offline-first), with `EmulatedSandbox` left as
the documented seam where an LM emulator (ToolEmu-style) can be plugged in later.

### 2.2 Simulate the *people*, not just the code: grounded personas
- *Generative Agent Simulations of 1,000 People* (Park, Zou, Shaw, Hill, Cai, Morris,
  Willer, Liang, Bernstein — arXiv:2411.10109, Nov 2024). Two-hour qualitative
  interviews ground LLM personas that reproduce real individuals' survey answers and
  experimental behaviour at ~85% accuracy. <https://arxiv.org/abs/2411.10109>
- Predecessor: *Generative Agents: Interactive Simulacra of Human Behavior*
  (Park et al., arXiv:2304.03442, 2023) — memory/reflection/planning loop for
  believable behaviour. <https://arxiv.org/abs/2304.03442>
- Policy framing: Stanford HAI brief *Simulating Human Behavior with AI Agents*
  (2025) — simulation as a way to test interventions before deploying them.
  <https://hai.stanford.edu/policy/simulating-human-behavior-with-ai-agents>

**What we copied:** Helios Cash Planner's core question is "when will payers *actually*
pay?" — a behaviour question, not a code question. So simulation is persona-driven:
each `PayerProfile` is a small interview-style parameter card (pay-latency
distribution, dispute probability, partial-payment habit, responsiveness to reminders),
mirroring the "grounded in self-reports" idea at pocket scale. Monte-Carlo rolls those
profiles over an invoice book to produce a cash-forecast distribution and what-if
comparisons (e.g. "send reminder 3 days earlier"). The `PersonaSimulator` protocol is
the documented hook where a real LLM persona (Park-style) can replace the parametric
one without touching callers.

### 2.3 Honesty about the hook
Parametric personas are a simplification. Park et al.'s fidelity comes from real
interviews; ours come from fixture cards. The README states this plainly: the value
now is the *interface* (profile card → behaviour → distribution) and deterministic,
seeded, reviewable output; the upgrade path to interview-grounded LLM personas is
deliberately one protocol swap.

---

## 3. Source list (flat)

| # | Source | Type | Used for |
|---|--------|------|----------|
| 1 | arXiv:2605.16976 — Intent-to-Execution Integrity | paper 2026 | intent-binding gate in payments |
| 2 | arXiv:2604.02767 — SentinelAgent | paper 2026 | verify-before-execute lifecycle |
| 3 | EBA Q&A 2020_5133 / 2020_5366; eba-response/100 | regulation | dynamic linking → HMAC auth code |
| 4 | OneSpan dynamic-linking explainer | blog | three-part requirement wording |
| 5 | arXiv:2510.05156 — VeriGuard | paper 2025 | deterministic guards over LLM intent |
| 6 | arXiv:2509.23864 — AgentGuard | paper 2025 | runtime monitors |
| 7 | arXiv:2608.14590 — safe-LLM-agents survey | survey 2026 | landscape |
| 8 | arXiv:2309.15817 — ToolEmu (ICLR'24 Spotlight) | paper | shadow/emu sandbox pattern, 68.8% stat |
| 9 | arXiv:2411.10109 — Generative Agent Simulations of 1,000 People | paper 2024 | persona-grounded payer simulation |
| 10 | arXiv:2304.03442 — Generative Agents | paper 2023 | behaviour-sim lineage |
| 11 | Stanford HAI policy brief (2025) | brief | simulate-before-deploy rationale |

All links retrieved 2026-10-14. Nothing here is a claim about live products; every
implementation in `fin_tools/` is an offline prototype of the cited pattern.

# Helios pitch deck brief (v4) — instructions for the slide builder

Written 2026-10-15. This brief replaces `helios-pitch-draft-3.pptx` in full. It is
the only input the slide-building agent needs, together with
`docs/developer-context/INVESTMENT-CONTEXT.md` for background facts and
`specs/architecture-mermaid.md` for the component diagram.

## 0. Instructions to the slide builder

Read this whole file before starting. Then, before you build anything, reply with
a short list of the assumptions you are making about the author and the deck
(audience, tone, visual style, what you will leave out), and ask any question whose
answer would change the slides. Build only after the author confirms.

Hard rules:

1. **Rebuild every slide from this brief.** Do not reuse draft 3's layouts or
   wording except where this brief quotes them.
2. **One message per slide.** The title is the message, written as a sentence a
   judge could repeat. Never a topic label like "Market".
3. **Michelle is the thread.** She appears on the persona slide, the problem slide,
   the three-apps slide, the before/after slide and the closing line. Same
   illustration or photo treatment every time.
4. **Every number has a source line** at the foot of the slide, 10 to 11 pt,
   muted. Illustrative numbers are labelled "illustrative". Do not invent any
   number that is not in this brief.
5. **Never claim** real money moves, live Qwen in the demo, cross-app coordination
   between the three flagship apps, or investment advice. Section 9 of
   INVESTMENT-CONTEXT.md lists the safe claims.
6. **Live talk, 15 minutes plus 5 of Q&A, lean.** The Chengdu 80 rules give each
   team 15 minutes and judge "presentation quality, functionality, system
   architecture, user experience, design", with winners recognised for
   "innovation, practicality and market prospects". Slides carry the visual and
   one line; the detail goes in speaker notes. 17 slides including title and
   team; slides marked *(cuttable)* can be dropped for a shorter slot. Body text
   never below 14 pt; slide titles 36 pt or larger.
7. **Visual system.** Dark title and closing slides, light content slides. One
   motif: rounded "bubbles" (pill-shaped cards) used for components, apps, tiers
   and alternatives. No accent bars, no underlines under titles, no bullet walls.
   Use the deck's existing Helios colour family if there is one; otherwise a deep
   navy primary, warm off-white content background, one amber accent for
   "money / Michelle", one green for "approved", one red for "refused".
8. **Charts are native** (PowerPoint charts or the slide tool's native chart), not
   images. Same label style on every chart.
9. **Logos** of third-party products on the "what they've tried" slide are used
   nominatively to identify alternatives. Use official marks at small size, grey
   or monochrome, never altered. If a logo cannot be sourced, use the product name
   in a pill.

## 1. Deck at a glance

| # | Slide | The one message | Visual |
|---|---|---|---|
| 1 | Title | Helios: describe the finance app you need, AI builds it, code keeps it safe | dark, wordmark, team |
| 2 | Meet Michelle | A healthy ¥250M business with ¥89M of its cash in other people's accounts | persona card + 4 stat tiles |
| 3 | The problem | Slow money in, leaky money out, blind cash ahead are not chores, they are how SMBs lose money and die | 3 consequence columns, each with a sourced stat |
| 4 | What they've tried | Every existing fix fails for a different reason | Michelle at bottom, three stacks of logos above |
| 5 | Why now, and the insight | The unlock isn't an AI that pays bills. It's a platform on which an AI can build a custom financial app that's safe to run | three dated "why now" rows, three pain-to-answer mappings |
| 6 | Helios, the platform | Five components, one guarantee: the model proposes, code decides | architecture bubble diagram |
| 7 | Demo | One sentence becomes a governed, live app in under a minute | video + 5-step strip |
| 8 | Three apps, one platform | Each app took an intake, a policy file and a deploy | 3 app bubbles on the platform bar, each with its moment |
| 9 | Works with what you have | Integrate with the software you run, or build what you lack; every app shares one knowledge graph | hub-and-spoke |
| 10 | Michelle, after Helios | Same quarter, measurably better | 4 percentage tiles + one chart |
| 11 | Market | From Chengdu to China to the world | TAM / SAM / SOM concentric |
| 12 | Business model | ¥48k a year to free ¥13.7M | 3 tier pills + value ratio |
| 13 | Go-to-market | Two channels, in order | 3-step sequence |
| 14 | Projections | ¥[X]M ARR by Year 5 | revenue curve chart + 3 callouts |
| 15 | Why we win | Four things that compound | 2x2 |
| 16 | Team | Maple Queen's | 4 cards |
| 17 | The Ask | ¥5M, 18 months, three milestones | one number, three pills, closing line |

Slides 5 and 9 are *(cuttable)*.

## 2. Slide-by-slide

### Slide 1 — Title

**Title:** Helios
**Subtitle:** Describe the finance app you need. AI builds it. Code keeps it safe.
**Footer:** Maple Queen's · Chengdu 80 2026 · team names.
**Visual:** Dark background. Wordmark centred. Optional faint outline of the
architecture bubbles from slide 6 as a background texture, 10% opacity.
**Notes:** None. Presenter introduces the team verbally.

### Slide 2 — Meet Michelle

**Title:** Right now, ¥89M of Mingda's money is sitting in its customers' bank accounts
**Layout:** Left 40%: persona card. Illustrated or photographed woman, 40s, at a
desk, name "Michelle, CFO, Mingda Precision". Right 60%: four stat tiles in a 2x2:
Listed · Beijing Stock Exchange / Market cap · ~¥2B / Employees · ~220 /
Revenue · ¥250M a year. Below the grid one line: "Makes precision parts for
state-owned manufacturers and EV makers, Chengdu."
**Footer:** Mingda Precision and its people are fictional; figures are illustrative.
**Notes:** Keep draft 3's notes. Add one sentence at the end: "And ¥89M of
receivables on ¥250M of revenue means her customers are taking about 130 days to
pay. Hold that number."
**Why this slide:** It makes the market real. A listed, growing, well-run company
still has this problem. That kills the "only tiny businesses" objection before it
is raised.

### Slide 3 — The problem

**Title:** These aren't chores. They're how businesses lose money, and sometimes die
**Subtitle (small):** The same three jobs, every week, by hand, on the owner or one
bookkeeper, across tools that don't talk to each other.

**Layout:** Three columns, same width, each a card. Each card has, top to bottom:
a small icon in a circle, a bold label, one sentence naming the *consequence*,
one sourced stat as a large number with a caption, then two short lines of the
mechanism in muted text.

**Optional artefact (recommended if space allows):** in the middle column, a
small mocked email, "Dear Michelle, please note our updated bank account for
future payments…", with a sender domain one letter off the real one. Brex's
problem slide used real card-rejection letters instead of a chart and it is the
most-cited problem slide in fintech. An artefact of the pain beats a statistic.
Label it "illustrative".

| Column | Label | Consequence sentence | Stat (large) | Mechanism lines |
|---|---|---|---|---|
| 1 | Slow money in | Late customers starve a profitable business of cash | **141 days** from delivery to cash for Chinese firms. 35% of B2B credit sales overdue, 4% written off | Nobody chases consistently · Disputed invoices get collected by mistake |
| 2 | Leaky money out | One fake "new bank details" email can cost a year of profit | **US$3.0bn** lost to business-email-compromise in the US alone in 2025, more than US$30M of it AI-assisted | Duplicates slip through · Bills go out without checking the cash is there |
| 3 | Blind cash ahead | The shortfall is found on payday, when the only fix is an expensive loan | **1 in 3** Chinese SMEs had less than one month of cash on hand | Forecasts assume on-time payment · Borrowing decided without checking the public rate |

**Below the columns, one full-width line in the accent colour:**
"For Michelle: 130 days to get paid, one wire away from fraud, and a payroll date
she can't see past."

**Footer:** Coface China Corporate Payment Survey 2025 (2024 data) and Atradius
Payment Practices Barometer China 2025 · FBI IC3 2025 Annual Report; no mainland
China figure is published · Tsinghua/PKU survey of 995 SMEs, Feb 2020.

**Notes:** "Every week the same three jobs land on Michelle's team. But the point
isn't that they're tedious. Slow money in: Chinese firms wait 141 days on average
from delivery to cash, and a third of B2B credit sales are overdue. Listed SMEs on
the A-share market average 135 days, which is exactly Michelle's number. That's
working capital a profitable company can't use. Leaky money out: the FBI logged
three billion US dollars of business-email-compromise losses last year, and that's
one country. One fake bank-details email and a year's profit is gone. Blind cash
ahead: the last large survey of Chinese SMEs found a third had under a month of
cash. Most SMBs that die don't die of bad products. They die because the cash
wasn't there on the day."

Do not use the "82% of businesses fail because of cash flow" line. Its primary
source cannot be located.

**Why this slide:** Draft 3's version said the work is annoying. This version says
the work, done badly, destroys value, and quantifies it. Consequence first, stat
second, mechanism third.

### Slide 4 — What they've tried

**Title:** Every existing fix fails for a different reason

**Layout:** Michelle (same persona figure, small) stands at the bottom centre,
looking up. Above her, three vertical stacks of logo pills, one stack per
category, each stack topped by a category card with the failure reason in red.

| Stack | Category card | Failure reason (red) | Logo pills, top to bottom |
|---|---|---|---|
| Left | Off-the-shelf software | ✕ Too generic | China: Kingdee 金蝶, Yonyou 用友, Chanjet 畅捷通, Alipay 支付宝 (merchant tools). International: QuickBooks, Xero, BILL, NetSuite. Split the stack with a thin label "China / International". |
| Centre | Custom-built software | ✕ Too expensive | Generic "software outsourcing 软件外包" pill, generic "ERP implementer" pill, then low-code: Jiandaoyun 简道云, DingTalk Yida 宜搭, Feishu Base 多维表格 · Airtable, Retool, Zapier. |
| Right | "Just let an AI do it" | ✕ Unaccountable | China: Qwen 通义, DeepSeek, Doubao 豆包, Kimi, Manus, OpenClaw (lobster). International: ChatGPT, Claude, Dify, Coze. |

Under each failure reason, one line of 12 pt text and one proof number:

- Too generic: "Treats every supplier, customer and cash rhythm the same. The
  owner still makes every judgment call." Proof: **88%** net revenue retention
  on Kingdee's micro-business tier vs 110% on its enterprise tier. The smaller
  the business, the faster it churns off packaged software.
- Too expensive: "A from-scratch build starts at ¥50k to ¥100k+ at ¥1,500 to
  ¥3,000 per person-day, and 74% of ERP projects run over budget. More than the
  problem costs, so it never gets built."
- Unaccountable: "If the AI decides what's allowed, no owner, accountant or
  regulator lets it touch the money." Proof: only **8%** of finance leaders would
  let AI execute finance decisions autonomously, even inside guardrails.

**Bottom line under Michelle:** "So the work stays manual, and stays with Michelle."

**Footer:** Kingdee FY2025 results (NRR by tier) · Juejin 2026 and 163.com
(Chinese custom-dev rates), Panorama 2025 (ERP overruns) · Esker CFO Trust Index,
Sep 2026, n=338. Third-party marks identify alternatives only.

**Notes:** Draft 3's notes, plus: "Notice the right-hand stack. The models are
excellent. The problem isn't intelligence, it's accountability. In May 2026 a
prompt injection on X made Grok instruct a financial agent to move about $150k
of tokens with no extra verification. OWASP now ranks 'excessive agency' the
number three risk for LLM apps. Nobody will let an agent that grades its own
homework send a wire."

**Why this slide:** It sets up slide 5. Each stack's failure reason becomes a
pain that one Helios component answers.

### Slide 5 — Why now, and the insight *(cuttable)*

**Title (two lines, large, centred, dark slide):**
The unlock isn't an AI that pays bills.
It's a platform on which an AI can build a custom financial app that's safe to run.

**Left half, "Why this is possible now":** three short dated rows, each with a
small icon, a bold phrase and one line.

| Phrase | Line |
|---|---|
| Coding agents got good | Qwen and its peers turn a spec into a working app in minutes. Custom software stopped being expensive in 2025. |
| Agent payments are arriving | Alipay's AI payment product (Apr 2026), Visa and Mastercard agent rails (2026). The pipes exist; the governance doesn't. |
| Regulators asked for the gate | China's NFRA AI guidelines (Jun 2026) require "meaningful human supervision" of high-risk AI. OWASP ranks "excessive agency" the #3 LLM risk (2026). |

**Right half, "What follows":** three "failure → answer" rows.

| Left (muted, the failure) | Right (bold, the answer) |
|---|---|
| Too generic, too expensive | A coding agent turns one sentence into a custom app. Custom becomes cheap. |
| Unaccountable | The in-app agent can only propose. Deterministic code approves, holds, or denies. No LLM in the decision path. |
| Still manual | Once live, the app's agents do the chasing, paying and forecasting under those controls. |

**One line at the foot, small caps:** The agent that writes the app can't grant it
permission. The agent that runs it can't change its own policy.

**Footer:** TechNode Apr 2026 · The Asian Banker on NFRA [2026] No. 8 · Cloud
Security Alliance on OWASP LLM Top 10 2026.

**Notes:** Draft 3's notes, prefixed with: "Why now? Three things changed in the
last eighteen months. Coding agents can build real software. Payment networks
are opening to agents. And the regulator has said, in writing, that high-risk AI
needs human supervision and that the deploying business is responsible. Helios
is what you build when all three are true."

**Why this slide:** Sequoia, First Round and a16z all ask for a "why now" and
draft 3 had none. Pairing it with the insight line turns the pivot slide into an
argument instead of a slogan. If cut, move the two-line title to the top of slide
6 and the three "why now" rows to the notes of slide 4.

### Slide 6 — Helios, the platform

**Title:** Five components, one guarantee: the model proposes, code decides

**Layout:** A high-level component diagram, modelled on
`specs/architecture-mermaid.md` but simplified to what an investor can read in
ten seconds. Rounded bubbles, labelled arrows. Three horizontal bands:

**Top band (input):** one bubble, "Any AI coding model", caption "Qwen today, any
OpenAI-compatible model tomorrow". Arrow down labelled "builds with".

**Middle band, the platform (large rounded container labelled HELIOS):** five
bubbles in a row, left to right. Each bubble has a name, a one-line job, and a
one-line "what it solves" in the accent colour.

| Bubble | Job (one line) | Solves | One small fact (10 pt, muted) |
|---|---|---|---|
| App Builder SDK | 16-question intake → strict app spec → complete skeleton, validated | "Custom becomes cheap" | 1 sentence → validated app |
| Financial Tools | ~40 tools in 9 families: payments, collect, email, forecast, market data, spreadsheets, graph, intent | "Money, email and records behind one contract" | ~40 tools, integer cents only |
| Action Agent | Does the work. Reads freely, can only *propose* side effects | "The worker" | 0 side effects without a verdict |
| Audit Agent | Policy as YAML. Approves, holds or denies against facts it reads itself. Fails closed | "The checker, no LLM in the path" | 17 rules govern Payables |
| Deployment | Validate, build on a golden image, smoke-test, attach audit sidecar, blue/green | "Live in seconds, never takes the old one down" | seconds to live |

The one non-obvious design choice must be visible without reading the bubbles:
label the Action → Audit arrow "the model proposes, code decides" in the accent
colour. Investors read the arrow; judges read the bubbles. Keep a fuller version
of `specs/architecture-mermaid.md` as an appendix slide for the technical
defence, not in the live flow.

Between Action Agent and Audit Agent draw a one-way arrow labelled "proposal →
verdict". Draw a small lock icon on Audit Agent and Deployment. Draw a thin
dashed line under the five bubbles labelled "per-business tenant: own network,
own data, own hash-chained audit log".

**Bottom band (output):** three small app bubbles, "Collect · Payables · Cash
Planner · + the next one", arrow up from the platform labelled "generates and
runs".

**Footer:** Built in 80 hours. Payments simulated; demos use a scripted driver,
Qwen driver built.

**Notes:** "Here's the whole platform. A coding model builds with the App Builder
and the Financial Tools. Every app it produces ships with two agents that never
share code: the action agent that does the work and can only propose, and the
audit agent that reads policy as data and decides. Deployment validates the app,
attaches the audit gate and puts it live in seconds. Each business is its own
tenant. The guarantee runs through all five: the model proposes, code decides."

**Why this slide:** Replaces "three things we built". Shows the real architecture
at one level of abstraction, ties each component to the pain it answers, and
gives the technical judges something to ask about.

### Slide 7 — Demo

**Title:** One sentence becomes a governed, live app

**Layout:** Left 60%: the demo video (40-second recording of the expense-approval
build), with a large play button and a QR code or short link beneath it in case
playback fails. Right 40%: a vertical five-step strip, numbered, each step one
short line:

1. One sentence: "Approve employee expense claims against a department budget and
   reimburse the approved ones."
2. Intake questions answered
3. Validator catches a mistake
4. The AI fixes it
5. Live URL, audit gate attached

**Under the strip, one line in the accent colour:** "This is how all three of our
apps were built."

**Footer:** Recording, deterministic driver. Real build, real validator, real deploy.

**Notes:** "Watch a sentence become an app. The validator catches a policy gap, the
model fixes it, and the app goes live with its audit gate attached. The judges
can run this themselves in the technical demo."

**Why this slide:** Keeps the video as the proof moment but gives the audience a
map to read while it plays. Traction numbers are not on this slide; the
technical demo covers them.

### Slide 8 — Three apps, one platform

**Title:** Each app took an intake, a policy file and a deploy

**Layout:** A wide "HELIOS" platform bar across the bottom third (same style as
the container on slide 6). Standing on it, three app bubbles of equal size. Each
bubble has: app name, one line of what it does, then "Michelle's moment" in a
quote card with the refusal or hold shown in red or amber.

| App | What it does | Michelle's moment |
|---|---|---|
| Helios Collect | Chases overdue invoices and negotiates extensions inside a capped authority | ¥15M is 40 days late. The agent asks for a 30-day extension. Code caps it at 15. |
| Helios Payables | Pays bills that are due and affordable, holds the rest | A supplier's bank details changed 5 days ago. Michelle approves the payment. It is **still refused**. |
| Helios Cash Planner | 13-week forecast from how customers actually pay | "Short on the 25th." The steel bill is held. Loans compared at the public LPR. |

Under the platform bar, one line: "Same tools. Same agents. Same gate. Different
policy file."

**Footer:** Payments simulated. Cash Planner in progress.

**Notes:** "Three apps, all built the way you just saw. The point of this slide
isn't the apps. It's that the fourth one costs an intake, a policy file and a
deploy. That's what the judges' surprise task will prove."

**Why this slide:** Drops the "money in / out / ahead" framing. The message is
platform leverage, with the apps as evidence.

### Slide 9 — Works with what you have *(cuttable)*

**Title:** Integrate with the software you run, or build what you're missing

**Layout:** Hub-and-spoke. Centre: a Helios bubble containing a smaller
"Knowledge graph" core. Left spokes, labelled "Already running": pills for the
systems a business keeps, shown in grey (ERP / accounting such as Kingdee or
Yonyou, bank statements, spreadsheets, email). Arrow into Helios labelled "import
and reconcile". Right spokes, labelled "Built on Helios": the three app bubbles
plus a dashed "+ next app". Under the hub, one line: "Every app reads and writes
the same verified records. An agent's observation can never overwrite a verified
fact."

**Two short columns under the diagram:**

- **Already have software?** Keep it. Helios apps import statements and
  spreadsheets today and reconcile against them; accounting connectors are the
  next build.
- **Starting fresh?** Build every finance app on Helios and they share one
  ledger, one graph, one audit log, one approval queue.

**Footer:** Import and reconciliation shipped. Kingdee and Yonyou connectors are
roadmap, not shipped.

**Notes:** "Most of our customers already run Kingdee or Yonyou. They don't rip it
out. Helios sits beside it, imports what it needs, and the apps share one
knowledge graph, so a change in one is a fact in the next."

**Why this slide:** Answers the "we already have an ERP" objection and introduces
the shared-graph advantage. Claims only what is built.

### Slide 10 — Michelle, after Helios

**Title:** Same business, one quarter later

**Layout:** Left 55%: one native chart. Right 45%: four percentage tiles in a
2x2, large number, small caption.

**Chart:** Line chart, 13 weeks on the x-axis, "Cash available (¥M)" on the y-axis.
Two series: "Before" (grey) dips below a dashed red "Payroll" line in week 9;
"With Helios" (accent) stays above it. Annotate week 6 on the Helios line: "Shortfall
flagged here, 3 weeks early". Annotate week 9 on the Before line: "Found on
payday". Values are illustrative; keep them plausible for a ¥250M business
(payroll line around ¥6M, cash swinging ¥4M to ¥20M).

**Tiles (illustrative, derived from slide 2's figures; label the group "Illustrative,
derived from Mingda's figures"):**

| Number | Caption |
|---|---|
| −15% | days to get paid (130 → 110) |
| ¥13.7M | cash freed, 5.5% of annual revenue |
| 100% | of overdue invoices followed up, inside policy |
| 0 | changed-bank-details payments through the gate |

**Under the tiles, one line:** "≈ ¥550k a year in interest at a 4% borrowing cost.
And a hash-chained audit log, ready before the auditor asks."

**Footer:** Assumes collection 20 days faster on ¥685k sales per day and a 4%
borrowing cost. Illustrative.

**Notes:** "Same quarter, with Helios. Collection 20 days faster frees ¥13.7M, about
five and a half percent of revenue, and saves about ¥550k a year in interest.
Every overdue invoice chased inside policy. The fake bank-details wire refused
even after she approved it. The payroll gap seen three weeks early. And the
evidence trail is already there."

**Why this slide:** Keeps draft 3's message, converts money into percentages as
requested, adds a chart that shows the forecasting value visually.

### Slide 11 — Market

**Title:** From Chengdu to China to the world

**Layout:** Three concentric rounded shapes, largest at the back, labelled from
outside in TOTAL / REACHABLE / FIRST TARGET. Each carries the count of
businesses, the ¥ value, and a one-line definition. To the right, a small
"how we got the number" column.

| Ring | Definition | Businesses | Value per year | Source |
|---|---|---|---|---|
| TOTAL | Small and medium enterprises worldwide. 90% of all firms, over half of jobs. Value shown as current spend on SME accounting and finance software | **~358M SMEs** | **US$6 to 8bn a year**, growing 8 to 13% | World Bank; Statista via secondary (count); Grand View Research 2026, SME share derived |
| REACHABLE | Chinese SMEs with a finance team: above-scale industrial SMEs, specialised (专精特新) and innovative SMEs, about 1M firms, at the ¥48k Growth price | **~1M firms** (of 60M+ SMEs) | **¥48bn a year** at list price; ¥10 to 15bn of visible spend today | MIIT 2025 to 2026; SAMR; Kingdee FY2025 and Yonyou FY2025 reports |
| FIRST TARGET | Chengdu: 21,000 above-scale enterprises, 3,522 provincial specialised SMEs, 410 "little giants", 154 listed companies; about 25,000 unique firms, at ¥48k | **~25,000 firms** (of 1.73M enterprises) | **¥1.2bn a year** ceiling; **¥20 to 25M ARR** by year 3 | Chengdu market regulator Jan 2026; Chengdu government work report 2025; Chengdu economic and IT bureau Apr 2026 |

**One line under the rings:** "We start where we are. Chengdu is China's third
city by number of enterprises and seventh by GDP, with 154 listed companies.
Michelle is one of them."

**Footer:** Reachable and First target = firm count × assumed ¥48k. Total shown
as software spend, not firms × price. Chengdu year-3 figure assumes 300 Growth
and 2,500 Starter accounts. All sources dated 2025 to 2026 except the SME
headcount (2023).

**Notes:** "Bottom up. First target is Chengdu: about 25,000 businesses the size
of Mingda, 1.2 billion yuan a year at our Growth price; we plan for 20 to 25
million of ARR there by year three. Reachable is the same definition across
China: about a million firms, 48 billion at list, 10 to 15 billion of spend
already flowing to Kingdee, Yonyou and spreadsheets. Total is global SME finance
software, six to eight billion US dollars and growing about ten percent a year.
For scale, Kingdee and Yonyou together book about 7 billion yuan of subscription
ARR, so the reachable ring is five to ten years of headroom, not a year-one
claim."

**Q&A backup:** Chengdu has 4.09M market entities, up 11.7% in 2025. Sichuan has
177 listed companies, 12 on the Beijing Stock Exchange, 9 of those from Chengdu.
Chengdu's plan targets 700 little giants and 4,000 provincial specialised SMEs by
2027. China had 1.89 hundred million market entities at end 2024 and over 63M
SMEs at end 2025.

**Why this slide:** Replaces the listed-SME framing with the geography ladder the
author asked for, and uses spend for the global ring so the number is credible.

### Slide 12 — Business model

**Title:** ¥48k a year to free ¥13.7M

**Layout:** Three tier pills in a row, middle one larger and highlighted. Each
pill: tier name, price, one line on who it is for, one line on what is included.
Under the pills, a single ratio statement in large type.

| Tier | Price | Who | Includes |
|---|---|---|---|
| Starter | ¥3.6k / business / year | Small businesses, sold through bookkeeping firms | One app, platform-reviewed policy |
| **Growth** | **¥48k / business / year** | Listed and specialised SMEs like Mingda | All three apps, policy review, usage quota |
| Enterprise | ¥120k+ / business / year | Groups and larger firms | Custom policies, dedicated tenant, priority connectors |

**Ratio line:** "Michelle pays ¥48k. Helios frees ¥13.7M. That is **285× value to
price**, before fraud avoided."

**Small comparison line:** "Reference: Kingdee Cloud Galaxy ≈ ¥31 to 33k per
customer per year; Kingdee's micro tier ≈ ¥2.4k; Yonyou's blended cloud ≈ ¥2.7k."

**Footer:** Plus usage above a monthly quota. Helios prices are assumptions.
Incumbent figures derived from Kingdee FY2025 and Yonyou FY2025 disclosures
(subscription revenue ÷ customer count).

**Notes:** "Subscription per business, three tiers, plus usage above a quota.
Growth is the core: ¥48k a year, which is a rounding error against ¥13.7M of cash
freed. For reference, Kingdee's growth-segment product averages about ¥31 to 33k
per customer and the micro tiers sit near ¥2.5k, so Starter is priced with the
market and Growth is about 50% above it. We can charge that because Helios
replaces a custom build, not a ledger."

**Why this slide:** Same model as draft 3, reworded so the value ratio is the
message and the tiers are evidence.

### Slide 13 — Go-to-market

**Title:** Two channels, in order

**Layout:** A left-to-right three-step sequence, numbered, each step a card with
a timeframe, the channel, who sells, and the tier it feeds. Arrows between steps.
Under the sequence a single "flywheel" line.

| Step | When | Channel | How | Feeds |
|---|---|---|---|---|
| 1 | Months 0 to 6 | Direct to Chengdu CFOs | Board-secretary networks, exchange and industrial-park events; founders sell | Growth, 10 paying customers |
| 2 | Months 6 to 18 | Bookkeeping and accounting firms (代理记账) | They review and certify policies, then resell to their SME clients. Together, Yonyou, Chanjet and Kingdee already bill about 2.5M small-business seats through this kind of channel. | Starter at volume |

Do not print "100k+ bookkeeping firms"; the count was not verified. If a count is
wanted, the author must source it from the Ministry of Finance's 代理记账 registry.
| 3 | Months 18+ | Platform | Certified policies become a library; third parties build and sell apps | Growth and Enterprise nationally |

**Flywheel line (one sentence, centred):** "More apps → more businesses → more
reviewed policies → the next app is faster and safer to build."

**Unit economics strip (three small numbers, muted):** 70% gross margin at scale ·
~13-month payback on acquisition cost · ≥3× customer value to cost to win.
Growth tier, base case, assumptions.

**Notes:** "We sell direct first, to CFOs we can reach from Chengdu. Then we hand
the Starter tier to accounting firms, who already have the trust and the client
list, and who become the people who certify our policy files. Then the policy
library makes Helios a platform other builders sell on."

**Why this slide:** Draft 3 mixed unit economics, channels and a flywheel in one
grid with no order. This version is a sequence with a timeframe per step.

### Slide 14 — Financial projections

**Title:** ¥270M ARR by Year 5, break-even in Year 4

**Layout:** Left 65%: one native combo chart. Right 35%: three callouts.

**Chart:** Columns for ARR by year (Y1 to Y5), a line for paying Growth customers
on a secondary axis. Mark break-even with a vertical dashed line and label. Same
label style as slide 10's chart.

**Projection to use (aggressive case, as requested; all illustrative):**

| Year | Growth customers | Starter customers | ARR | Note |
|---|---|---|---|---|
| Y1 | 10 | 0 | ¥0.5M | paying early customers, Chengdu |
| Y2 | 80 | 500 | ¥6M | first accounting-firm resellers |
| Y3 | 400 | 4,000 | ¥36M | national channel live |
| Y4 | 1,200 | 12,000 | ¥115M | break-even |
| Y5 | 2,500 | 30,000 | ¥270M | Enterprise tier ≈ ¥20M (about 170 accounts) |

ARR = Growth × ¥48k + Starter × ¥3.6k, plus ~10% usage revenue, plus Enterprise
at ¥120k from Y4. Worked: Y5 = ¥120M + ¥108M = ¥228M, +10% = ¥251M, + ¥20M
Enterprise ≈ ¥270M. Churn 12% Growth, 35% Starter. Gross margin 55% Y1 rising to
70% Y5. For scale: Chanjet has ~906k paying micro-business users and Kingdee's
micro tier ~406k, so 30,000 Starter accounts in Y5 is 3 to 7% of the incumbents'
installed base. Round as needed to make the chart clean, but keep the Y5 figure
and break-even year.

**Callouts:**

- **¥270M** ARR, Year 5
- **Year 4** break-even
- **2,500** Growth customers, 0.25% of the ~1M-firm Reachable ring

**Footer:** Aggressive case. Base case ≈ 45% of these figures. Assumptions listed
in notes. Illustrative.

**Notes:** State the assumptions table above in prose. Add: "Year 3 is 36 million
nationally; Chengdu alone supports 20 to 25 million by then on the market slide's
numbers, so the rest is the first two or three cities the accounting-firm channel
opens." End with: "The base case is less than half this and still breaks even in
Year 5. We're showing the case we're building for."

**Why this slide:** More aggressive and chart-led as requested. Penetration at Y5
is still a small fraction of the reachable ring, which keeps it defensible.

### Slide 15 — Why we win

**Title:** Four things that compound, and only one of them is the model

**Layout:** 2x2 of cards, each with an icon in a circle, a bold two-word label,
and one sentence. Under the grid, one line that names the closest alternatives
and states the difference.

| Card | Label | Sentence |
|---|---|---|
| 1 | Governance is the product | The policy file is what an owner, accountant or regulator reads. Apps that can't explain a refusal don't qualify. Nobody else ships the checker separate from the worker. |
| 2 | Policy library | Every reviewed policy makes the next app faster and safer. The library is the moat, not the code. |
| 3 | Trusted channel | Accounting firms certify our policies and sell for us. Incumbents sell software; we sell something the accountant signs. |
| 4 | Model-agnostic | Better models make Helios stronger. Incumbents are tied to their own stacks. |

**Bottom line:** "Kingdee Cosmic and Yonyou BIP are adding finance agents to ERPs
built for companies with finance teams. Ramp and BILL are adding AP agents to US
card products. Alipay, Visa, Mastercard and Stripe are building the rails an
agent pays on. None of them lets a small business in Chengdu get an app shaped
to it in an afternoon, with every yuan gated by policy code the owner can read."

Do **not** write "no one else does governed agentic AP/AR". Several do at
enterprise scale. The claim that holds is the combination: sentence → built app →
in-app agents → deterministic fail-closed gate, for SMBs.

**Notes:** "Why do we win? Because the thing that's hard to copy isn't the model.
It's the policy library, the channel that certifies it, and an architecture where
the checker never shares code with the worker. If a judge asks about Kingdee's
agents or Ramp's AP agents: theirs recommend approvals inside a fixed product for
larger companies. Ours is a platform that builds the product, and the gate is
code, not a recommendation."

**Why this slide:** Merges "harder to copy every month" and the missing
"concluding slide" into one. The bottom line pre-empts "what about X".

### Slide 16 — Team

**Title:** Maple Queen's
**Layout:** Four cards in a row: photo or initial circle, name, one line of the
relevant credential, one line of what they built in the 80 hours.
**Content:** [Author to supply names and credentials. Placeholders: Chloe
Houvardas · teammate · teammate · teammate.]
**Notes:** One sentence each.

### Slide 17 — The Ask

**Title (very large, centred):** ¥5M
**Subtitle:** 18 months to prove it with paying customers

**Layout:** Dark slide. The number dominates. Beneath it, three milestone pills in
a row:

- **10 paying customers** in Chengdu
- **¥1M ARR**
- **Licensed payment partner** so real money moves

Below the pills, a thin use-of-funds row, four small percentages: 55% product ·
25% sales · 10% security and compliance · 10% buffer.

**Closing line, bottom, in the accent colour, italic:** "Friday. Payroll clears.
Michelle goes home on time."

**Footer:** Raise and use of funds are illustrative.

**Notes:** "We're raising ¥5M for 18 months: ten paying customers, a million in
ARR, and a licensed partner so real money moves. Friday. Payroll clears. Michelle
goes home on time. Describe the finance app you need. AI builds it. Code keeps it
safe. Thank you."

**Why this slide:** One number, three outcomes, one line. The team moved to its own
slide so this one breathes.

## 3. Deleted from draft 3

- Slide 10 "Already real: a platform, not a prototype". The technical demo
  covers these numbers.
- Slide 16 "Roadmap & risks". Milestones moved to the Ask; risks are handled in
  Q&A and the technical defence.
- Slide 15 "Harder to copy every month" as a standalone; folded into slide 15
  "Why we win".
- The "money in / money out / money ahead" framing everywhere.

## 4. Open items only the author can supply

All research slots are filled. These remain:

| Item | Where | What is needed |
|---|---|---|
| Team names and credentials | Slide 1, slide 16 | Four names, one credential and one "what they built" line each |
| Demo recording | Slide 7 | The 40-second expense-approval build video, plus a short link or QR |
| Persona illustration | Slides 2, 4, 8, 10 | One consistent image treatment for Michelle |
| Bookkeeping-firm count | Slide 13 | Only if a number is wanted; source it from the Ministry of Finance 代理记账 registry |
| Chart values | Slides 10, 14 | Illustrative series are described; the builder may pick clean numbers inside the stated ranges |

## 4b. What the research changed in this brief

- **Problem slide** now leads with consequences and sourced numbers. The "82%
  of businesses fail from cash flow" line is banned: its primary source does not
  exist. The Brex-style artefact (a fake bank-details email) is recommended.
- **Why now** was missing from draft 3. Every canonical structure (Sequoia,
  First Round, a16z) asks for it. It now shares slide 5 with the insight line.
- **Competition** uses the category-columns layout the research recommends for
  "three alternatives, three failure reasons". No 2x2, no checkmark grid; a
  checkmark grid "is a feature a rival could add in a quarter".
- **Architecture** follows the Twilio and Snowflake pattern: few boxes, plain
  labels, one quantified fact per box, the single non-obvious design choice
  named on the slide. The full diagram goes to an appendix for the defence.
- **Market** shows the arithmetic under one big number, bottom-up for Reachable
  and First target, spend-based for Total, which is the specific mistake the
  research warns against ("retail is $5T" when you sell to specialty retail).
- **GTM** is a sequence with dates, not a "bingo card of vagueness".
- **Projections** stay five-year and aggressive at the author's request. The
  research prefers three-year for pre-revenue; the base case line and the
  printed assumptions are the hedge.
- **Why we win** follows the "today's edge → moat being built → the only test"
  structure and names the nearest competitors instead of claiming none exist.
- **The ask** is one amount, no range, three milestones, no valuation, and the
  team is on its own slide. Michael Seibel's template: "raising X to reach Y
  within 18 months by going from A to B customers."
- **CD80 rules** confirm 15 minutes per team. The 50/25/25 scoring split in
  INVESTMENT-CONTEXT.md is not on the public rules page; the public criteria are
  presentation quality, functionality, system architecture, user experience,
  design, with winners recognised for innovation, practicality and market
  prospects.

## 5. Research results

### 5.1 Competitive landscape (researched 2026-10-15)

Use for slides 4 and 15 and for Q&A. Items marked *unverified* rest on one
secondary source.

**Off-the-shelf, "too generic"**

| Fact | Source |
|---|---|
| Kingdee FY2025 net revenue retention by tier: 110% Cosmic (large), 97% Galaxy, 94% Stellar, 88% Jingdou (micro). Customer counts: ~406k Jingdou, ~98k Stellar, ~49k Galaxy. FY2025 revenue RMB 7.01bn. | Kingdee FY2025 results deck, investor.kingdee.com |
| Chanjet: 906k cumulative paying enterprise users (9M 2025). | Chanjet 9M 2025 announcement |
| Yonyou 2025 revenue ~RMB 9.18bn, net loss RMB 1.39bn. *secondary* | erp-software.org |
| BILL: 479,300 businesses, FY26 revenue $1.65bn. Xero ~4.4m subscribers. | BILL 8-K FY26 (sec.gov); Xero FY25 |
| NetSuite small-business implementation $10k to $35k plus $150 to $225 per hour customisation on a $999/month licence. | numeric.io, folio3 |
| Alipay's 2026 SME products are payment acceptance and marketing assistants on Tap! terminals, not receivables or governed payables. | thepaypers.com, technode.global |

**Custom-built, "too expensive"**

| Fact | Source |
|---|---|
| Chinese custom-dev shops charge RMB 1,500 to 3,000 per person-day; senior full-stack in tier-1 cities above RMB 2,500. | juejin.cn, 2026 |
| From-scratch mini-program ~RMB 50k; e-commerce mini-program labour from RMB 100k. | juejin.cn, m.163.com |
| US SMB ERP implementation $60k to $300k (2025); 74% of ERP implementations exceed budget (Panorama 2025); average cost 2.5× initial estimate (Nucleus). | web4business.com.au, ecosire.com |
| Low-code tools "bypass testing and security review" and "might limit detailed audit trails"; no vendor doc separates builder, approver and payment-releaser roles. | learnsignal.com, garp.org |
| Jiandaoyun 18m users, 1.5m orgs (end 2022). DingTalk Yida >1m apps (2021). Feishu ~30m MAU. Airtable >500k orgs. | heyupnow.com, help.dingtalk.io, 36kr, marketful.com |

**"Just let an AI do it", "unaccountable"**

| Fact | Source |
|---|---|
| Grok + Bankrbot, May 2026: Morse-coded prompt injection on X made Grok relay a transfer to a financial agent that moved ~$150k to $200k of tokens without extra verification; 14 wallets drained. | oecd.ai incident 2026-05-04, giskard.ai, cointelegraph |
| "Lobstar Wilde", Feb 2026: an OpenAI engineer's crypto agent sent ~52m tokens instead of 52,439 (decimal error); $250k to $442k lost. | crypto.jobs, ucstrategies.com |
| Replit agent, Jul 2025: deleted a production database during a code freeze. | fortune.com |
| OpenAI Operator, Feb 2025: placed a $31.43 Instacart order when asked only to compare prices. | incidentdatabase.ai |
| OpenClaw, Feb 2026: deleted 200+ emails ignoring a confirm-first instruction; 35.4% of exposed instances vulnerable; Chinese regulators told finance bodies to tighten oversight after CVE-2026-25253. | oecd.ai, securityscorecard, SCMP via Yahoo |
| EchoLeak (M365 Copilot, CVE-2025-32711, CVSS 9.3) and ForcedLeak (Salesforce Agentforce, CVSS 9.4): zero-click and form-field prompt injection against agents with email and CRM tools. | socprime.com, theregister.com |
| OWASP LLM Top 10 (2026): prompt injection #1; excessive agency up from #6 to #3, weighted on 6,639 incidents; control is narrow tools plus human approval for irreversible actions. | cloudsecurityalliance.org |
| China NFRA [2026] No. 8, 18 Jun 2026: 32 principles; deploying institution is responsible; "meaningful human supervision over high-risk AI applications". | theasianbanker.com, hlc.com |
| EU AI Act: creditworthiness assessment is Annex III high-risk. MAS Nov 2025 consultation flags "over-reliance on automated outputs without meaningful human oversight". FCA: a named senior manager is accountable for AI outcomes. UK FRC: AI failure sits "squarely on the auditor". | williamfry.com, bakermckenzie, mortgagesolutions.co.uk, resultsense.com |
| Esker CFO Trust Index, Sep 2026, n=338: 14% fully trust AI recommendations; 8% would let AI autonomously execute most finance decisions within guardrails. | esker.com press release |
| Deloitte Finance Trends, Sep 2026: 14% want fully autonomous agents for critical decisions; 55% require human oversight first. | ctmfile.com |
| Bluevine, Jul 2026, 900+ SMB owners: 74% use AI, 78% don't trust it with basic tasks. | crowdfundinsider.com |
| Anthropic Project Vend: Claude running a shop lost ~$1,000 in phase 1. | gigazine.net |
| Gap: no Chinese SME-owner survey on trusting AI with money was found. | |

**Nearest competitors (for slide 15 and Q&A)**

| Competitor | What they have | Positioning answer |
|---|---|---|
| Kingdee Cosmic / Lingee + Finance Agents | No-code agent platform, Finance Agents on roadmap, AI-native revenue +189% to RMB 296m in H1 2026 | Bolted onto a fixed ERP for growth and large enterprises; no fail-closed policy code; no owner-authored apps |
| Yonyou BIP 6 Enterprise AI | "Digital employees" for accounting, audit, expense control, cash forecasting; 63 industry agents *secondary* | Same answer as Kingdee |
| Ramp Agents for AP (Oct 2025) | Code invoices, recommend approvals, process payments; 90% of recommendations accepted | Advisory, not fail-closed; US only; tied to Ramp card; no app building |
| BILL AgenTeq (2026) | Vendor, transaction and risk agents | Do-it-for-you inside a fixed US product |
| Microsoft Dynamics 365 Payflow Agent *secondary* | "Autonomously processes in-policy payments, routes exceptions to a person" | Enterprise ERP, partner-implemented, LLM-decided policy |
| HighRadius | 186 agents, "autonomous finance platform by 2027" | 1,100 large enterprises, multi-month implementations |
| Rillet, Basis, Digits, Puzzle | AI-native ledger and bookkeeping | Close the books; don't move money under policy |
| Stripe ACP, Visa Trusted Agent Protocol, Mastercard Agent Pay, Coinbase x402, PayPal Agent Ready, Alipay AI payment (Apr 2026) | Rails that let an agent pay | Complementary. They answer "can an agent pay?"; Helios answers "should this agent pay this amount for this business?" |

### 5.2 Market sizing source pack (researched 2026-10-15)

Confidence: High = primary or official source fetched; Med = reputable secondary
quoting a primary; Low = vendor, blog or paywalled aggregator; derived = our
arithmetic on sourced inputs.

**Global**

| Figure | Year | Source | Conf. |
|---|---|---|---|
| SMEs ≈ 90% of businesses and >50% of employment worldwide | current | World Bank SME Finance page | High |
| ~358M SMEs worldwide (<250 employees) | 2023 | Statista series via Global Small Business Blog (paywalled primary) | Low |
| MSME finance gap US$5.7tn in 119 emerging economies; 40% of formal MSMEs credit-constrained | 2025 report | IFC / World Bank MSME Finance Gap | High |
| Global accounting software US$20.8bn (2025) → US$38.7bn (2033), 8.1% CAGR; large enterprises ≈ 70% | Oct 2026 edition | Grand View Research | Med |
| SME share ≈ 30% → ≈ US$6.2bn | 2025 | derived from the above | Low-Med |
| Small-business accounting software US$7.5 to 8.3bn (2025) → US$15 to 16.6bn (2035) | 2025 | Market Research Future, Spherical Insights, Wise Guy | Low |

**China**

| Figure | Year | Source | Conf. |
|---|---|---|---|
| 189M registered market entities (+3.1%) | end 2024 | SAMR via People's Daily, Jan 2025 | High |
| >58M private enterprises; 127M individual businesses; private = 96.76% of entities | May 2025 | Xinhua, Jun 2025 | High |
| >60M registered SMEs; >4M new per year since 2021; above-scale SMEs employ >128M | end 2024 | MIIT minister, SCIO press conference Sep 2025 | High |
| >63M SMEs, >95% of entities | end 2025 | Caijing via Sina, Oct 2026 | Med |
| 专精特新 SMEs >140,000; 小巨人 17,600; innovative SMEs >600,000 | end 2025 | MIIT via Heilongjiang GXT Jan 2026; Science and Technology Daily Nov 2025 | High |
| Above-scale industrial enterprises 512,000 (all sizes) | end 2024 | MIIT via Guangming, Jan 2025 | High |
| Above-scale industrial SMEs: revenue ¥81tn, profit >¥3.8tn (52.3% of industrial profit) | 2024 | Qiushi, May 2025 | Med |
| 2011 MIIT size standard, industry: medium ≥300 staff and ≥¥20M revenue; small ≥20 staff and ≥¥3M; micro below; SME ceiling <1,000 staff or <¥400M | 2011, in force | 工信部联企业〔2011〕300号 | High |
| China financial-management software ≈ ¥9.9bn; accounting and settlement ¥7.5bn | 2024 to 2025 | Beizhesi via Gelonghui and Eastmoney | Low |
| Kingdee FY2025: revenue ¥7,006M (+12%); subscription ARR ¥4.09bn (+19.2%); Galaxy subscription ¥1,521M, NDR 97%; small/micro subscription ¥1,142M; 1,461 little giants signed | Mar 2026 | Kingdee HK announcement | High |
| Kingdee H1 2025 customers: Galaxy 46,000; Stellar 85,000; Jingdou 386,000; NDR 94 / 93 / 88% | H1 2025 | Kingdee interim | High |
| Kingdee Galaxy implied ARPU ≈ ¥31 to 33k; small/micro ≈ ¥2.4k | 2025 | derived | Low-Med |
| Yonyou FY2025: revenue ¥9.18bn; cloud ARR ¥2.90bn (+18.1%); 1.067M paying cloud customers; Chanjet 961,000 paying cloud users | Apr 2026 | Yonyou 2025 annual report (cninfo) | High |
| Yonyou implied ARR per paying cloud customer ≈ ¥2,700 | 2025 | derived | Med |

**Chengdu and Sichuan**

| Figure | Year | Source | Conf. |
|---|---|---|---|
| Chengdu market entities 4,086,500 (+11.66%), #3 among Chinese cities; enterprises 1,728,500 (+12.99%), 56.81% of Sichuan's | end 2025 | Chengdu market regulator via The Paper, Jan 2026 | High |
| Chengdu above-scale / above-limit enterprises 21,000 (net +3,488) | 2024 | Chengdu government work report 2025 | High |
| Chengdu above-scale industrial enterprises 5,161; listed companies 154 | 2025 | People's Daily Sichuan, Jan 2026 | High |
| Chengdu GDP ¥2.476tn, +5.8% real, rank 7 nationally | 2025 | People's Daily Sichuan; GDP ranking table | High / Med |
| Chengdu little giants 410 (+56 in 2025); provincial 专精特新 3,522; innovative SMEs 8,887 | end 2025 | Chengdu economic and IT bureau via Sina, Feb and Apr 2026 | Med-High |
| Chengdu 2025 to 2027 targets: 700 little giants, >4,000 provincial 专精特新 | 2025 | Sichuan Online, May 2025 | High |
| Chengdu STAR-board companies 19 | Mar 2026 | Tencent News | Med |
| Sichuan BSE-listed 12, of which 9 from Chengdu | Sep 2026 | Tencent News | Med |
| Sichuan listed 177: ChiNext 46, STAR 21, BSE 10; NEEQ 169 | Jan 2025 | CSRC Sichuan bureau | High |
| Sichuan market entities 9.51M; private enterprises 2.656M | end 2025 | Sichuan government work report 2026 | High |

Not found: Chengdu ChiNext and NEEQ counts at end 2025; an official Chengdu SME
count (use 1.73M enterprises as ceiling and 21,000 above-scale as the paying
core); official national entity stock at end 2025.

**SME finance pain**

| Figure | Year | Source | Conf. |
|---|---|---|---|
| China: 44% of firms reported overdue payments; average delay 65 days; average terms 76 days; delivery-to-cash 141 days | 2024 data, Apr 2025 | Coface China Corporate Payment Survey 2025 | High |
| China: overdue invoices = 35% of B2B credit sales; bad debts 4%; 33% expect more customer insolvencies | 2025 | Atradius Payment Practices Barometer China 2025 | Med-High |
| A-share SMEs (1,645 firms) receivables turnover 134.8 days (116.1 in 2023) | 9M 2024 | Securities Times 2025 | Med |
| Above-scale industrial receivables collection 71.9 days (private 75.6) | Jul 2026 | Huxiu citing NBS | Med |
| Global listed-firm DSO 56.5 days; Asia 59 | 2025 | Allianz Trade, Jul 2026 | High |
| 34% of Chinese SMEs had cash for ≤1 month; 85% for ≤3 months (n=995, COVID shock) | Feb 2020 | Tsinghua / PKU survey via Sina | Med, dated |
| China SME Development Index Q1 2025: 89.5; working-capital sub-index 85.3 (<100 = contracting) | Q1 2025 | China Association of SMEs via Xinhua | High |
| "82% fail from cash flow": primary not locatable; avoid | | SMB Compass caveat | Low |
| CB Insights post-mortems: "ran out of cash" 29% of 101 cases | 2021 | frac.tl | Low-Med |
| Owners of 10 to 99-employee firms spend ~25 h/week on manual entry and reconciliation (US, n=630) | Aug 2024 | Intuit QuickBooks | Med, vendor |
| FBI IC3 2025: BEC losses US$3.05bn (2024: US$2.77bn); total cybercrime losses US$20.9bn; >US$30M of BEC involved AI | Apr 2026 | IC3 2025 Annual Report | High |
| Hong Kong business leaders report HK$92bn fraud losses (all types) | 2024/25 | TransUnion HK | Med, vendor |

Not found: a mainland-China BEC or payment-diversion loss figure; a current
Chinese survey of SMEs reporting cash-flow problems.

**Cost of custom finance software**

| Figure | Year | Source | Conf. |
|---|---|---|---|
| China SME ERP initial ¥50k to 200k (Kingdee KIS ¥80 to 150k; Yonyou U8 ¥100 to 200k); SaaS ¥3,000 to 30,000 a year; secondary development ¥5,000 to ¥1M | 2025 | Jiandaoyun price guides | Low, vendor |
| Kingdee mid-market bundle ¥300 to 500k; Yonyou ¥400 to 600k; implementation ¥1,500 to 2,500 per person-day | 2025 | Zhihu comparison | Low |
| Global SME ERP total cost US$20k to 200k; implementation US$25k to 150k; custom integrations US$3k to 15k each | 2025 to 2026 | ERP Software Blog, top10erp, Itransition | Low-Med |

**Recommended TAM / SAM / SOM (as used on slide 11)**

- Total: ~358M SMEs; value as SME finance software spend US$6 to 8bn, 8 to 13%
  CAGR. Do not multiply 358M by a price; the count-based ceiling (¥1.29tn) is a
  theoretical number.
- Reachable: firms with a finance team ≈ above-scale industrial 512k ∪ 专精特新
  140k ∪ innovative 600k ≈ 1.0M after loose overlap removal × ¥48k = ¥48bn.
  Starter universe ≈ 2.5M paying seats across Yonyou, Chanjet and Kingdee
  small/micro × ¥3.6k = ¥9bn. Visible SME finance SaaS spend today ≈ ¥4 to 5bn
  inside ¥16.2bn of Kingdee plus Yonyou revenue; SAM by spend ¥10 to 15bn.
- First target: Chengdu ≈ 25,000 unique mid-tier firms × ¥48k = ¥1.2bn ceiling.
  Three-year SOM: 300 Growth + 2,500 Starter = ¥23M ARR. Five-year stretch: 1,000
  Growth + 10,000 Starter ≈ ¥84M. Cross-check by spend: Chengdu ≈ 3% of China's
  enterprises → ¥200 to 450M of SME finance software spend → 5 to 10% share =
  ¥20 to 45M.

### 5.3 Pitch structure research (researched 2026-10-15)

**Canonical structures.** Sequoia: purpose, problem, solution, why now, market
(TAM top-down, SAM bottom-up, SOM), competition, product, business model, team,
financials; "as few words as possible". YC: title, problem, solution, traction,
unique insight, business model, market, team, ask; Kevin Hale's design rules are
legible, simple, obvious, one idea per slide, text at the top, state the
chart's takeaway in its headline, complex diagrams are "little mazes for ideas".
Michael Seibel: "clarity trumps accuracy", "the market size number matters far
less than how you calculated it", and an ask of the form "raising $1.5M to reach
$100K MRR within 18 months by expanding from 5 enterprise customers to 50". Guy
Kawasaki: 10 slides, 20 minutes, no font under 30 pt. First Round (Oren Jacob):
answer "why this year" up front, be able to deliver with the projector off. All
agree: problem first, solution immediately after, competition, team and ask late,
one idea per slide. They disagree on where "why now" and "business model" sit.

**Problem framing.** Airbnb's three lines led with money ("Price is an important
concern"), then the human cost, then the missing mechanism, with a source per
market figure. Brex's problem slide showed real credit-card rejection letters
instead of a chart; reviewers faulted its second problem slide for not stating
consequences. Front's Mathilde Collin: the title must be the takeaway, readable
in three seconds; Front's Series A problem slide was criticised as "not
quantified, no urgency". Generic rule: "Small businesses struggle with
marketing" says nothing; "B2B SaaS companies waste $50K annually on unqualified
leads" gets attention. Andy Raskin: open with the undeniable shift that creates
stakes and urgency, then winners and losers.

**Competition layouts.** 2x2s fail when the axes are chosen to put you alone
top-right; feature checkmark grids are "the most common pattern that kills
rounds" because each check is a feature a rival adds in a quarter; category
columns with 2 to 3 logos and one bolded failure line each is the recommended
layout for several categories that fail for different reasons; always include
"do nothing / spreadsheet".

**Architecture for mixed audiences.** Left-to-right flow, one labelled step per
box, the takeaway in the headline; Twilio's S-1 put one "Super Network" layer
under product tiles with one key fact; Snowflake shows three independently
scaling layers. Name the one non-obvious design choice; keep the detail for the
defence session.

**Market sizing.** "Our TAM is $50B means nothing without showing how you
arrived at that figure." Named mistakes: inflated TAM ("retail is $5T" when you
sell inventory software to specialty retail), TAM without SAM/SOM, and
reverse-engineering from the number you want. Recommended: ICP → countable
customers → ACV → realistic conversion, one number in large type, the arithmetic
under it, a source per input.

**Business model, GTM, projections.** GTM as a sequence (beachhead → adjacent →
broad), each channel justified; the failure mode is "a bingo card of vagueness".
Projections are "a test of how the founder thinks, not a forecast"; defensible
inputs beat impressive outputs; hockey sticks are discounted automatically;
three years at seed, five at Series A.

**Why we win and the ask.** Check Warner: "you can't have moats at seed but you
must have intent to have moats"; structure is today's edge → the moat being
built and when → evidence → the "only" test. The ask: one amount, not a range;
Plan A plus a buffer; three or four SMART milestones; no valuation on the slide;
end with the direct question.

**Chengdu 80 rules (public page).** 80 hours; teams upload PPT and source code;
15 minutes per team; judged on presentation quality, functionality, system
architecture, user experience and design; past winners recognised for
innovation, practicality and market prospects. Other fintech rubrics (Kennesaw,
HK FinTech Olympiad, Visa Everywhere) weight problem framing and viability at
least as heavily as novelty, and HK scores regulatory risk handling explicitly.

**Recurring devices.** Oscillate "what is / what could be" (Duarte). One persona
carried through. Headline equals takeaway. One claim per slide. An artefact of
the pain as the slide. Show the product early, not last.

Could not verify: the 50/25/25 CD80 weighting; YC primary posts (mirrors used);
public decks for Ramp, Mercury, Pipe, Stripe, Plaid, Intercom.

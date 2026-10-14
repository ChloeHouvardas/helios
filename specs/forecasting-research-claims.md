# Research Claims to Verify (Forecasting & Simulation)

**Purpose:** every factual claim from the forecasting-methods discussion that could end up in specs, docs, or pitch slides. Verify the "unverified" rows via web access before we commit them to project material.

**Status legend:** ✅ verified (checked against the linked/primary source) · ⚠️ substance holds but citation or wording needs correction · ❌ incorrect — do not use as written

**Verification passes:**
- Pass 1 — 2026-10-14, C1–C3 marked verified against linked sources.
- Pass 2 — 2026-10-14, full sweep: all remaining rows checked against primary sources (arXiv abstract pages, publisher/Crossref metadata, official library docs), S12 executed locally, non-web claims checked against the repo. **Result: 3 claims need correction before use (C7 ❌, S3 ❌, C2 ⚠️ parameter range); 10 more need citation or wording fixes.** Details in §Corrections.

---

## Core claims (load-bearing — used to justify design decisions)

| ID | Claim | Source | Verify at | Status |
|---|---|---|---|---|
| C1 | Ablation studies show removing the LLM component from LLM-based time-series forecasters does not hurt performance (often improves); simple baselines match them; no few-shot benefit. NeurIPS 2024 **Spotlight**. | Tan, Merrill, Gupta, Althoff, Hartvigsen — "Are Language Models Actually Useful for Time Series Forecasting?" | https://arxiv.org/abs/2406.16964 | ✅ (drop the word "Fair") |
| C2 | Chronos is a family of pretrained probabilistic time-series forecasters based on T5 (**8M–710M** params), tokenizing series via scaling+quantization; trained by Amazon; strong zero-shot results. | Ansari et al. — "Chronos: Learning the Language of Time Series" | https://arxiv.org/abs/2403.07815 | ⚠️ (was 20M–710M — wrong lower bound) |
| C3 | Constrained decoding can enforce JSON/grammar structure on LLM output via finite-state-machine vocabulary indexing, with little overhead; implemented in the Outlines library. | Willard & Louf — "Efficient Guided Generation for Large Language Models" | https://arxiv.org/abs/2307.09702 | ✅ (see caveat on Outlines' current standing) |
| C4 | Conformal prediction gives distribution-free coverage guarantees from held-out calibration errors, applicable at small n. | Vovk, Gammerman & Shafer, *Algorithmic Learning in a Random World* (2005); tutorial: Angelopoulos & Bates, *Foundations and Trends in Machine Learning* 16(4), 2023 | https://arxiv.org/abs/2107.07511 | ✅ (fix journal abbreviation) |
| C5 | Standard (split) conformal coverage weakens under distribution shift / non-exchangeability (time series); weighted remedies exist. | Barber, Candès, Ramdas, Tibshirani — "Conformal prediction beyond exchangeability", *Annals of Statistics* 51(2):486–507 (2023) | https://arxiv.org/abs/2202.13415 | ✅ ID correct |
| C6 | Empirical-Bayes / partial pooling (shrinkage toward group means) provably dominates naive per-group estimation in **total squared-error risk when estimating ≥3 groups**. | Efron & Morris — "Stein's Paradox in Statistics", *Scientific American* 236(5):119–127 (1977); Gelman et al., *Bayesian Data Analysis* 3rd ed. ch. 5 | https://doi.org/10.1038/scientificamerican0577-119 | ⚠️ reworded — "small-n" was wrong framing |
| C7 | Hazard models are standard for time-to-event prediction in credit risk. **Discrete-time**: Shumway (2001), bankruptcy timing, handles censoring. **Continuous-time** proportional hazards: Cox (1972). | Shumway — "Forecasting Bankruptcy More Accurately: A Simple Hazard Model", ***The Journal of Business*** 74(1):101–124 (2001); Cox — "Regression Models and Life-Tables", *JRSS-B* 34(2):187–220 (1972) | https://doi.org/10.1086/209665 | ❌→✅ journal, DOI and Cox framing all corrected |
| C8 | The single-factor model in Vasicek (2002) underlies Basel II/III IRB portfolio capital; one common factor drives correlated defaults. | Vasicek — "The Distribution of Loan Portfolio Value", *Risk* 15(12), Dec 2002, pp. 160–162 | https://www.bis.org/publ/work/wp230.htm (Basel linkage) | ⚠️ substance ✅; pages not confirmed against the original issue |

## Supporting claims (cited in the survey; check before slides)

| ID | Claim | Source | Verify at | Status |
|---|---|---|---|---|
| S1 | TimesFM: decoder-only pretrained forecaster, **Google Research**. | Das, Kong, Sen & Zhou — "A decoder-only foundation model for time-series forecasting", ICML 2024 | https://arxiv.org/abs/2310.10688 | ⚠️ title is not "TimesFM"; ID is 2023, venue is 2024 |
| S2 | Moirai: universal forecasting transformer with any-series/any-frequency training, Salesforce AI Research. | Woo, Liu, Kumar, Xiong, Savarese & Sahoo — "Unified Training of Universal Time Series Forecasting Transformers", ICML 2024 | https://arxiv.org/abs/2402.02592 | ✅ |
| S3 | Lag-Llama: probabilistic foundation model for time series, **ServiceNow Research**. | **Rasul, Ashok et al.** — "Lag-Llama: Towards Foundation Models for Probabilistic Time Series Forecasting" (2023) | https://arxiv.org/abs/2310.08278 | ❌→✅ author was misattributed |
| S4 | TimeGPT: first foundation-model forecasting API, Nixtla. **Preprint only — not peer-reviewed.** | **Garza, Challu & Mergenthaler-Canseco** — "TimeGPT-1" (2023) | https://arxiv.org/abs/2310.03589 | ⚠️ three authors, not two |
| S5 | DLinear: linear models match/beat transformers on standard TS benchmarks. | **Zeng, Chen, Zhang & Xu** — "Are Transformers Effective for Time Series Forecasting?", AAAI 2023 | https://arxiv.org/abs/2205.13504 | ⚠️ models are DLinear + NLinear under "LTSF-Linear" |
| S6 | DeepAR: probabilistic autoregressive recurrent forecaster across many related series, Amazon. | Salinas, Flunkert, Gasthaus (+ Januschowski in the journal version), *Int. J. Forecasting* 36(3):1181–1191 (2020) | https://arxiv.org/abs/1704.04110 | ⚠️ author list differs between arXiv and journal |
| S7 | Temporal Fusion Transformer: interpretable multi-horizon probabilistic forecaster. | Lim, Arık, Loeff & Pfister, *Int. J. Forecasting* 37(4):1748–1764 (2021) | https://arxiv.org/abs/1912.09363 | ✅ |
| S8 | TabPFN: tabular foundation model with strong small-data performance (≤ ~10k samples, 500 features, 10 classes). **"v2" is an informal community label — the Nature paper does not use it.** | Hollmann et al. — "Accurate predictions on small data with a tabular foundation model", *Nature* 637(8045):319–326 (8 Jan 2025) | https://www.nature.com/articles/s41586-024-08328-6 | ✅ link resolves; drop "v2" from formal citations |
| S9 | Bayesian bootstrap: nonparametric alternative to Efron's bootstrap representing distributional uncertainty. | Rubin — "The Bayesian Bootstrap", *Annals of Statistics* 9(1):130–134 (1981) | https://doi.org/10.1214/aos/1176345338 | ✅ |
| S10 | Scenario deltas are **analogous to** interventions in Pearl's do-operator sense (edit the data-generating process, re-run). | Pearl, *Causality*, 2nd ed., CUP (2009); Pearl & Mackenzie, *The Book of Why*, Basic Books (2018) | books | ⚠️ change "=" to "analogous to" — see §Corrections |
| S11 | Kaplan-Meier nonparametric survival estimation handles censored observations. | Kaplan & Meier — "Nonparametric Estimation from Incomplete Observations", *JASA* 53(282):457–481 (1958) | https://doi.org/10.1080/01621459.1958.10501452 | ✅ |
| S12 | Sobol low-discrepancy sequences available and deterministic in scipy. **Method is `.random(n)`, `scramble` defaults to True, and n should be a power of 2.** | `scipy.stats.qmc.Sobol` | https://docs.scipy.org/doc/scipy/reference/stats.qmc.html | ✅ executed locally on scipy 1.17.0 |

## Tooling claims (verified against official docs)

| ID | Claim | Verify at | Status |
|---|---|---|---|
| T1 | vLLM supports JSON-schema-constrained ("structured") generation and runs Qwen models. **`guided_json` was REMOVED in v0.12.0 — use `response_format` / `structured_outputs`.** | https://docs.vllm.ai → Structured Outputs | ⚠️ capability ✅, our API mental model was stale |
| T2 | llama.cpp supports GBNF grammar-constrained generation; Qwen GGUF builds work with it. JSON-Schema→GBNF is now native C++; the old Python script is superseded. | https://github.com/ggml-org/llama.cpp → `grammars/README.md` | ✅ with schema-subset caveats |
| T3 | xgrammar provides grammar-constrained decoding compatible with Qwen (Qwen named explicitly in the README's supported-model list). Apache-2.0; default backend in vLLM. | https://github.com/mlc-ai/xgrammar | ✅ |
| T4 | PyMC supports seeded, reproducible MCMC via `random_seed` (int, array of ints, or `numpy.random.Generator`). Apache-2.0. Only relevant if we go beyond conjugate closed forms. | https://www.pymc.io → `pymc.sample` | ✅ with determinism caveats |

## Non-web claims (verified against the repo / competition brief)

- **Competition rules** (Qwen-only generative model, Tushare data, scoring weights 25/25/50, surprise task): `specs/helios-context.md` — ✅ **matches the external brief exactly.** Verified 2026-10-14 by line-by-line comparison against `Chengdu80/helios-context.md`: 113 lines each, zero content differences. The only divergence is line endings (external file LF, project copy CRLF) — cosmetic, no action needed unless a diff tool flags it.
- **"Forecast tools are read-only → bypass the audit gate":** ✅ **follows from the contract**, with one precision fix that matters for the architecture defense:
  - `specs/interfaces.md` §6 — each tool declares a side-effect flag, and "the side-effect flag determines whether a call runs directly or goes through the audit gate."
  - `specs/interfaces.md` §3 — "Read-only tool calls run directly. Tool calls with side effects become proposals and are not executed until reviewed."
  - `dev-resources/build1.md` — build order step 1 defines the tool contract with the side-effect flag; step 3 intercepts only side-effect calls into proposals.
  - **Precision fix:** bypassing the *audit gate* (approve/hold/reject) is not the same as being invisible to audit. §3 also states "Every tool call, proposal, verdict, and result is written to an append-only action log" — so a read-only forecast call is still logged. Say "bypasses the approval gate, still recorded in the action log." This is consistent with `specs/forecasting-simulation.md` §7 ("Forecasts are agent-generated observations, never authoritative records").
  - **Minor:** the literal field name `side_effect=false` is not yet pinned anywhere. §6 fixes the contract *shape*; the "Interfaces to Define Later" list still has "Financial tool schemas and permissions (contract shape agreed; individual tools still to be defined)." Don't cite `side_effect` as an agreed identifier.

---

## Corrections to apply before any of this reaches slides or specs

**C7 — the DOI and the journal were both wrong. Do not cite the old string.**
- `https://doi.org/10.1016/S0378-4266(00)00119-7` **does not resolve.** Crossref returns 404; the `10.1016` prefix is Elsevier, and no such article exists. Remove it everywhere.
- The journal was **not** *Journal of Banking & Finance*. Correct: Shumway, Tyler. "Forecasting Bankruptcy More Accurately: A Simple Hazard Model." ***The Journal of Business*** 74, no. 1 (January 2001): 101–124. https://doi.org/10.1086/209665 — every element confirmed against Crossref metadata.
- **Cox (1972) is continuous-time**, not discrete-time: "Regression Models and Life-Tables", *JRSS-B* 34(2):187–220, DOI 10.1111/j.2517-6161.1972.tb00899.x. Citing Cox as support for a *discrete-time* hazard model would be wrong. Shumway is the discrete-time source — his abstract reads "I describe a simple technique for estimating a discrete-time hazard model." Censoring is handled natively by the hazard framework (firms that survive without bankrupting are right-censored).
- Suggested claim wording: "Hazard models are standard for time-to-event prediction in credit risk; the discrete-time form (Shumway 2001) handles censored observations, building on Cox's (1972) continuous-time proportional-hazards model."

**S3 — wrong first author.** The paper is *not* by Ekambaram et al. Actual: "Lag-Llama: Towards Foundation Models for Probabilistic Time Series Forecasting", first authors **Kashif Rasul and Arjun Ashok (equal contribution)**, ServiceNow Research, arXiv:2310.08278. Origin of the mix-up: **Vijay Ekambaram is the first author of TSMixer (arXiv:2306.09364, IBM Research, KDD 2023) and Tiny Time Mixers (NeurIPS 2024)** — a different line of work. If we want to cite Ekambaram, cite TSMixer, not Lag-Llama.

**C2 — wrong parameter lower bound.** Released sizes are tiny **8M**, mini 20M, small 46M, base 200M, large 710M. The claim's "20M" is the *mini* model; the family floor is 8M. Everything else holds: all five Chronos-T5 sizes are T5-based (tiny derives from t5-efficient-tiny, vocabulary reduced 32,128 → 4,096 tokens); mean scaling + quantization into a fixed bin vocabulary is correct; probabilistic via autoregressive token sampling is correct; Amazon is correct; 42-dataset zero-shot result is correct. Note also: **arXiv preprint only, no peer-reviewed venue listed**, and successors exist (Chronos-Bolt, and Chronos-2 released Oct 2025) — cite the original for the method, but don't present it as current SOTA.

**C6 — the substance was imprecise, and "small-n" is the wrong reason.** Efron & Morris (1977) show James-Stein / empirical-Bayes shrinkage **dominates naive per-group estimation in total squared-error risk**, and the result requires **≥3 parameters estimated jointly**. It is a dimensionality condition, not a small-sample condition, and it is about *joint* risk across all groups — not guaranteed improvement for any single group. "Provably improves small-n grouped estimation" would not survive a technical judge. Use: "Shrinkage toward group means provably dominates naive per-group estimation in total squared-error risk when estimating three or more groups (Efron & Morris 1977)." Citation: *Scientific American* 236(5), May 1977, pp. 119–127, DOI 10.1038/scientificamerican0577-119. Gelman et al., *Bayesian Data Analysis* **3rd ed.** ch. 5 "Hierarchical models" is correct.

**C1 — drop the word "Fair."** The paper says "a series of ablation studies"; "fair" is our editorial gloss. The methodology does support the spirit of it (same datasets, metrics, evaluation protocol and hyperparameter budget across arms), but don't put a word in the authors' mouths on a slide. Spotlight status is solid — confirmed twice, from the arXiv abstract ("Accepted to NeurIPS 2024 (Spotlight)") and the official neurips.cc Spotlight Posters listing. Authors: Mingtian Tan, Mike A. Merrill, Vinayak Gupta, Tim Althoff, Thomas Hartvigsen (University of Virginia; MIT). The three ablated methods are GPT4TS/OneFitsAll, Time-LLM and LLMTime; the paper's own conclusion is that removing or replacing the LLM with a basic attention layer "does not degrade forecasting performance -- in most cases, the results even improve!"

**C4 — expand the journal abbreviation.** "*FnTML*" is non-standard. Correct: Angelopoulos, Anastasios N. and Stephen Bates. "A Gentle Introduction to Conformal Prediction and Distribution-Free Uncertainty Quantification." *Foundations and Trends® in Machine Learning* 16, no. 4 (2023): 494–591. DOI 10.1561/2200000101. arXiv 2107.07511 resolves correctly. Vovk, Gammerman & Shafer (2005) is correct; note a **2nd edition (Springer, 2022)** exists if we want the current one.

**C5 — confirmed, ID was right.** The team's "(check ID)" flag can be cleared. arXiv 2202.13415 = "Conformal prediction beyond exchangeability", Barber, Candès, Ramdas, Tibshirani (that order), *Annals of Statistics* 51(2):486–507, 2023, DOI 10.1214/23-AOS2276. Minor: the published title uses lowercase "exchangeability".

**C8 — substance confirmed; treat the page numbers as provisional.** Basel linkage is solid and sourced from authoritative material, not blogs: BIS Working Paper 230 (Tarasov 2007) on the Asymptotic Single-Risk-Factor model; the BIS *Explanatory Note on the IRB Risk Weight Functions* (2005); the Bank of England CCBS handbook ("the Vasicek loan portfolio value model…is the basis of the Basel II risk weight functions"). Basel III retains the ASRF foundation. Pagination **pp. 160–162** is supported by two agreeing secondary sources (FDIC WP 2007-06 and an econjournals citation) but **could not be checked against the original *Risk* issue** — if the deck needs certainty, cite "*Risk*, December 2002" without pages. Also worth knowing: the model originates in **Vasicek, "Probability of Loss on Loan Portfolio", KMV Corporation working paper, 12 Feb 1987**; the 2002 article is the widely-cited exposition.

**S10 — soften the equals sign.** Pearl's do-operator acts on a structural causal model with an explicit DAG; "edit the delta and re-run the engine" is a loose metaphor, not an application of do-calculus. Books cited correctly (*Causality* 2nd ed., CUP, 2009; *The Book of Why*, Basic Books, 2018). Write "**analogous to** / **inspired by** interventions in Pearl's sense" — presenting it as a literal equivalence invites a question we cannot answer in the defense.

**S1 — the paper is not titled "TimesFM".** Correct: Das, Kong, Sen & Zhou, "A decoder-only foundation model for time-series forecasting", **Google Research**, arXiv:2310.10688 (submitted Oct 2023), **ICML 2024**. "TimesFM" is the model name. Cite as "Das et al. (2024)" — the year refers to the ICML publication, not the arXiv ID.

**S4 — three authors, not two.** Azul Garza, **Cristian Challu**, Max Mergenthaler-Canseco, "TimeGPT-1", Nixtla, arXiv:2310.03589. The paper does claim to be the first production-ready pretrained forecasting model. Flag for slides: **arXiv preprint, no peer review**, and weights are API-gated — weak if cited as reproducible.

**S5 / S6 — minor citation hygiene.** S5: Ailing Zeng, Muxi Chen, Lei Zhang, Qiang Xu (Huawei Noah's Ark Lab / CUHK), AAAI 2023; the models are **DLinear and NLinear** under the umbrella **LTSF-Linear**, and the claim is about *long-term* forecasting benchmarks. S6: the arXiv version is Salinas, Flunkert, Gasthaus; the **journal version adds Tim Januschowski** — *Int. J. Forecasting* 36(3):1181–1191, 2020. Cite the journal version if we cite the year 2020.

**S8 — the link was fine; the label is the problem.** `https://www.nature.com/articles/s41586-024-08328-6` resolves correctly to Hollmann et al., "Accurate predictions on small data with a tabular foundation model", *Nature* 637(8045):319–326, 8 Jan 2025. The paper **does not call itself "v2"** — that is informal community usage, and later separate releases (TabPFN-2.5, TabPFN-3) exist. Cite "TabPFN (Nature 2025)". Stated operating envelope: up to ~10,000 samples, 500 features, 10 classes; prior-data-fitted-network based.

**S12 — verified by execution, and the docs alone would have misled us.** Ran on **scipy 1.17.0 / numpy 2.2.4**:
- `scipy.stats.qmc.Sobol` exists. In current scipy the sampling method is **`.random(n)`** — `.draw(n)` no longer exists (an `AttributeError`), so any sample code or older snippet using `.draw` must be updated.
- **`scramble` defaults to `True`.** Getting the plain deterministic Sobol sequence requires `scramble=False`; `scramble=True` with a fixed `seed` is reproducible. Both confirmed empirically (identical draws twice; different seed → different points).
- **n need not be a power of two, but should be.** `random(7)` returns a `(7, 3)` array and raises `UserWarning: The balance properties of Sobol' points require n to be a power of 2.` It does **not** silently truncate to the nearest power of two — plan sample sizes as `2**m` for the balance guarantee, and don't assume truncation.

**T1 — capability confirmed, but our API is out of date.** vLLM supports structured outputs with modes `json` (JSON schema), `regex`, `choice`, `grammar` (EBNF/CFG) and `structural_tag`. **The `guided_json` / `guided_regex` / `guided_choice` / `guided_grammar` sampling params were removed in v0.12.0** — current usage is `response_format` (OpenAI-compatible) or `structured_outputs` / `StructuredOutputsParams`. Backends are now **xgrammar (default)** and **guidance**; Outlines and lm-format-enforcer are no longer listed. Qwen support is explicit in the supported-models list (`Qwen2ForCausalLM`, `Qwen3ForCausalLM`, `Qwen3MoeForCausalLM`, `Qwen3NextForCausalLM`, plus VL/Omni variants). Gotcha: for Qwen3-Coder reasoning mode, structured outputs need `--structured-outputs-config.enable_in_reasoning=True`. Action: pin a vLLM version in `specs/tech-stack.md` and drop any `guided_json` reference.

**T2 — confirmed, with a schema-subset caveat that could bite us.** GBNF is still the primary grammar format (`--grammar` / `--grammar-file`; server accepts a `grammar` field or `json_schema` / `response_format`). JSON-Schema→GBNF conversion is now native C++ (`common/json-schema-to-grammar.cpp`, `--json`/`-j`); the older Python `json-schema-to-grammar.py` is superseded. Qwen official GGUF repos exist (`Qwen/Qwen2.5-*-GGUF`, `Qwen/Qwen3-*-GGUF`). **Documented schema limitations:** `additionalProperties` defaults to **false** (opposite of the JSON Schema spec), no `uniqueItems` / `contains` / `not` / conditionals, `pattern` must be anchored, and nested `$ref`s are broken. Deeply nested schemas can destabilise the server — keep generated-app output schemas flat. Action: whatever schemas Qwen emits must stay inside this subset, or constraint enforcement silently degrades.

**T3 — confirmed, including Qwen specifically.** XGrammar (MLC-AI) does grammar-constrained decoding for JSON, regex and custom CFGs; Apache-2.0; integrated into vLLM (as default backend), SGLang, MLC-LLM, TensorRT-LLM, OpenVINO GenAI and Modular MAX. The README's supported-model list **names Qwen explicitly**, and XGrammar builds per-tokenizer token masks, so the Qwen tokenizer is covered — this is a stated fact, not an inference. Caveat: it is tokenizer-specific, so it must be initialised with the exact Qwen tokenizer in use. Version detail (release line / dates) moves fast — pin whatever we adopt and re-check at implementation time rather than trusting the numbers recorded here.

**T4 — confirmed.** `pymc.sample(random_seed=...)` accepts an int, an array-like of ints, or a `numpy.random.Generator`; each step method gets its own independent generator, and legacy `RandomState` raises `TypeError`. Default sampler NUTS is deterministic given a seed. Apache-2.0. **Determinism caveats:** multi-chain runs need one seed per chain; BLAS/OpenMP thread count can affect floating-point reproducibility across architectures; alternative samplers (`nutpie`, `blackjax`, `numpyro`) need separate installs and carry their own guarantees. Relevant to `forecasting-simulation.md` §7 (audit agents re-running projections) and the §9 open question on versioning behaviour-model updates so old forecasts stay reproducible — if we ever add MCMC, seed discipline must be part of the recorded inputs.

---

## Two things worth deciding, now that the evidence is in

1. **C8 is the weakest link to our actual design.** C4/C5 → the percentile bands and shortfall probabilities in `forecasting-simulation.md` §3.2. C6 → segment priors and cold start (§3.5, §8). C7 → time-to-payment in the behavioural timing models (§3.5). Those four map cleanly onto the four method additions. **Vasicek's single-factor model is regulatory capital for loan portfolios, not cash-flow forecasting** — one common factor driving correlated defaults is a real analogue for correlated late payment across customers, but if we cite it as justification for a *cash shortfall* engine a judge may ask why we need a Basel capital model. Either spell out the analogy (shared macro factor → correlated payment delays → correlated shortfall risk) or drop C8 from the load-bearing set and keep it as background.

2. **The register claims C4–C8 "justify the four method additions" — that count checks out** (C4 and C5 both serve conformal prediction; C6, C7, C8 serve the other three). No missing or duplicate justification, so the priority list in the original file was sound. All five are now resolved: C4 ✅, C5 ✅, C6 ⚠️ reworded, C7 ❌→✅ corrected, C8 ⚠️ pages provisional.

**Bottom line:** nothing here blocks the design. The methods are all supported by real, correctly-identified literature. But C7 and S3 must be fixed before anything is printed, C2's parameter range and C6's wording must be fixed before a technical defense, and the T1/T2 API details must be reconciled with `specs/tech-stack.md` before implementation. Note that `specs/forecasting-simulation.md` is still marked "Proposal — pending team review" and is not yet reflected in `specs/decisions.md` or the architecture docs.

---

## Tushare claims (P2 adapter — added 2026-10-14, ALL UNVERIFIED)

The `services/financial-api/financial_api/market.py` adapter encodes assumptions about the Tushare Pro API that came from model memory, not from the official docs. Confirm each against https://tushare.pro/document/2 (or the interface-specific doc pages) before trusting live data; `api_name` and `value_field` are parameters precisely so a wrong name is a one-line fix.

| ID | Claim (as encoded in market.py) | Verify at | Status |
|---|---|---|---|
| T5 | The LPR interface is named `lpr` and returns fields `TRADE_DATE`, `LPR1Y`, `LPR5Y`. | Tushare docs: LPR/基准利率 pages | ⚠️ |
| T6 | The SHIBOR interface is named `shibor` and returns an overnight field `ON` (plus tenors like `1W`, `1M`). | Tushare docs: Shibor pages | ⚠️ |
| T7 | Requests are JSON POSTs to `http://api.tushare.pro` with body `{api_name, token, params, fields}`; responses are `{"code": 0, "data": {"fields": [...], "items": [...]}}`; non-zero `code` means an error. | Tushare Pro API overview | ⚠️ |
| T8 | Rates are quoted in percent (e.g. `3.45` = 3.45%); the adapter divides by 100. | Tushare docs / sample data | ⚠️ |
| T9 | Dates arrive as `YYYYMMDD` strings (e.g. `20261013`); the adapter also accepts `YYYY-MM-DD`. | Tushare docs / sample data | ⚠️ |

Also verify: whether the competition's Tushare access includes these interfaces (organizers provide Tushare covering market data, company financials, macro data per `specs/helios-context.md`).

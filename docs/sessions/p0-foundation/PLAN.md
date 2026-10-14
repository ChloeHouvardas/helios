# Session `p0-foundation` — plan

Objective: make the repository one consistent place to build on. No product
features. Three later sessions run in parallel on top of this commit, so leave
no half-finished state.

Worktree: `.qwen/worktrees/p0-foundation`, branch `worktree-p0-foundation`,
base `chloe-dev` @ `cc96f0a`.

## Baseline (observed before any change)

| suite | passed |
|---|---|
| platform/audit-agent-builder | 79 |
| platform/financial-tools | 86 |
| platform/agent-integration | 123 |
| apps/helios-collect | 74 |
| entry-point/examples/invoice-payer | 41 |
| **total** | **403** |

All five suites ran offline. Measured with `PYTHONPATH` injection into the
platform `src/` dirs, because the repo venv did not exist yet.

## Deviations from the task prompt (recorded, not re-litigated)

1. **The worktree did not exist.** The prompt describes
   `$MAIN/.qwen/worktrees/p0-foundation` on branch `worktree-p0-foundation` as
   already created by `qwen --worktree=p0-foundation`. Neither existed. Created
   both from `chloe-dev` @ `cc96f0a` with `git worktree add`. Local and
   reversible.
2. **`docs/research/` does not exist** in the main checkout (only
   `docs/milestones/`). Task 0 has nothing to commit; the verified facts in
   prompt section E are the authority instead.
3. **The prompt-set directory is not readable.** `helios-qwen-prompts/` outside
   the repo returns EPERM even with an explicit read grant, so the truncated
   tail of the task prompt (Task 7 onward) could not be recovered from source.
   Tasks 0–6 and Task 7's stated scope are executed from the attachment text.
4. **System `python3` is Anaconda 3.13.9, not 3.14.** It already has
   pytest/pydantic/pyyaml/jsonschema/httpx/jinja2/numpy, and lacks
   fastapi/uvicorn/docker/redis/python-multipart. The repo venv is built anyway,
   since Task 1 requires one environment; `python3.13` resolves to the same
   Anaconda 3.13.9 interpreter.
5. **Worktrees exclude gitignored files**, so the `.venv` dirs the prompt
   reports under `platform/action-agent-builder/` and
   `platform/agent-integration/` are present only in the main checkout, not
   here. They are unused by this plan.

## Numbered tasks

- [x] **T0** research notes — nothing to commit (see deviation 2).
- [x] **T1** one environment, one test command: fix the two un-installable
      pyprojects (`fin_tools` needs `[build-system]` + explicit packages;
      `agent_toolkit` drop unused `qwen-agent` and the script entry pointing at
      a non-existent `cli.py`), write `scripts/bootstrap-venv.sh`,
      `scripts/test-all.sh`, pinned `requirements-dev.txt`, extend `.gitignore`,
      replace every `python3 -m pytest` instruction with venv commands.
      Done when `scripts/test-all.sh` prints 403 passed.
- [x] **T2** import the deployment engine into `platform/deployment/` from
      `OTHER-TEAMMATE-STUFF/deployment-stuff/fPT9INmvWJ/default/helios/`,
      excluding `.venv/`, `.helios-run/` (holds a runtime secret), `engine.log`,
      `.DS_Store`. Add `pyproject.toml`, pin `requirements.txt`, fix
      `bootstrap.sh` Docker-socket detection order and drop stale Traefik lines.
      Add `tests/test_contracts.py` + `tests/test_orchestrator_offline.py`
      (docker-py mocked; no daemon required). Wire into `test-all.sh`.
- [x] **T3** import the four financial-tool teammates as sibling packages of
      `fin_tools`, tests under `platform/financial-tools/tests/` with
      package-prefixed filenames: `helios_forecast` (folder copy, newer, LF),
      `spreadsheets` (`origin/payments`, offline), `email_tool`
      (`OTHER-TEAMMATE-STUFF/email-tool/sender/`), `payments_collect`
      (`OTHER-TEAMMATE-STUFF/payments-tool/collect/`). One commit each, body
      credits the source. Expect roughly +68/+105/+38 plus spreadsheets.
- [ ] **T4** fix defects 1–6, one commit each, each with a test that fails
      before and passes after:
      1. `contracts.py:54` `REDACTED_KEYS` missing `auth_code`,
         `authorization_code` — a payment credential reaches the hash-chained
         audit log in full via `owner_question`.
      2. `runtime.py:131-132` `summary()` counts read-only calls as side
         effects; count only entries carrying a proposal.
      3. `runtime.py:611` hard-coded `name == "send_email"` → optional
         `records_contact: bool = False` on `agent_toolkit.contracts.
         ToolDefinition`, set on `make_send_email` and in
         `apps/helios-collect/tools.py`.
      4. `model_driver.py` re-target GLM-5.3 → DashScope Qwen; env order
         `HELIOS_LLM_API_KEY, DASHSCOPE_API_KEY, OPENAI_API_KEY`;
         `enable_thinking: false` only for `qwen3`-family ids; drop
         `thinking`/`reasoning_effort`; keep `stream: false`,
         `tool_choice: "auto"`, one-call-per-turn parse, `reasoning_content`
         ignore. Re-target `tests/test_model_driver.py`, `.env.example`,
         `README.md`, `SETUP.md`, `cli.py` docstrings.
      5. `check_app.py:41-43` docstring promises a side-effect-flag cross-check
         the code does not perform → implement it.
      6. `fixtures/invoices.csv` UTF-8 BOM → read with `utf-8-sig` in
         `spreadsheet_tools.load_csv`, strip the BOM from the file.
- [ ] **T5** record decisions (a)–(f) in `specs/decisions.md`, sync
      `specs/tech-stack.md`, list `platform/agent-integration` and
      `platform/deployment` as real components in
      `specs/architecture-mermaid.md` prose.
- [ ] **T6** correct the stale docs listed in prompt section E; rewrite
      `HANDOFF.md` §2, §4, §7 only.
- [ ] **T7** `QWEN.md` at repo root, ≤80 lines.

## Rules that bind every commit

- No push / force-push / rebase / amend / reset / history rewrite / `--no-verify`.
- No `git add -A` or `git add .`; stage explicit paths, check
  `git status --porcelain` first.
- Never touch `OTHER-TEAMMATE-STUFF/`, `docs/milestones/`, `.env`, `*.venv`.
- Never weaken a control to make a test pass. If a gate correctly refused, fix
  the test or the fixture.
- Never copy `.helios-run/tenants/demo.json` — it holds a runtime secret.
- Qwen is the only generative model allowed; remove GLM/Zhipu/bigmodel.cn
  references from any file touched.
- Each commit leaves `scripts/test-all.sh` green.

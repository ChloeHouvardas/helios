# WORKTREE.md — `p0-foundation`

A description of what this worktree contains and what it changed. Written for an
agent that will merge several worktrees together. **This file does not prescribe
how to merge** — it only reports what is here, what is new, and where this
worktree is likely to collide with others.

---

## 1. Identity

| | |
|---|---|
| Session | `p0-foundation` |
| Worktree | `<repo>/.qwen/worktrees/p0-foundation` |
| Branch | `worktree-p0-foundation` |
| Branched from | `chloe-dev` @ `cc96f0a` ("docs: add a repository handoff for a cold reader") — this is the true merge-base |
| Commits ahead of merge-base | 21, all local. Nothing pushed. |
| Diff vs merge-base | 134 files changed, +17623 / −566. **42 modified**, **92 added.** |
| Working tree | Clean at time of writing. |

**`chloe-dev` moved during this session.** It advanced from `cc96f0a` to `7ff4b74`
while this work was in progress, adding two commits this branch has never seen:

| Commit | Files |
|---|---|
| `b98ba36` "fix: stop reporting an outage as an audit-gate rejection" | `platform/financial-tools/fin_tools/service.py` (+38 / −2) |
| `7ff4b74` "feat: live Tushare market data behind an allow-list" | `fin_tools/market_data.py` (new, 587 lines), `fin_tools/__init__.py`, `tests/test_market_data.py` (new, 43 tests) |

None of this branch's work was deleted by that, and the merge-base is still
`cc96f0a`. See §7 for the overlap analysis — it is favourable.

Session notes live at `docs/sessions/p0-foundation/`: `PLAN.md` (numbered task
list, plus five recorded deviations from the task prompt) and `PROGRESS.md`
(running log with the reasoning behind the non-obvious calls). This file was
originally committed there and moved to the repo root so a merging agent finds it
without knowing the session name.

---

## 2. Objective

Make the repository a single consistent place to build on: one Python environment,
one test command, every teammate's untracked work imported into the tracked tree
with its tests green, the model driver pointed at Qwen, six known defects fixed,
stale docs corrected, and a root `QWEN.md` for later sessions to load.

**No product features were added.** Nothing here is a new capability an end user
would see; it is foundation work that later sessions build on. Three sessions were
expected to run in parallel on top of this commit, so the intent was to leave no
half-finished state.

---

## 3. What this worktree delivers

### 3.1 One environment, one test command

| Artifact | Purpose |
|---|---|
| `scripts/bootstrap-venv.sh` | Builds `.venv` on Python 3.13 and installs **five** first-party packages editable, then the pinned deps. Verifies all five import. |
| `scripts/test-all.sh` | Runs every suite in its **own** pytest process (per-suite `conftest.py` `sys.path` shims must not leak), prints `== <suite>: N passed`, exits non-zero on any failure. A suite that collects nothing counts as failure. |
| `requirements-dev.txt` | Every version pinned exactly, so two worktrees cannot drift into different dependency trees. |
| `.gitignore` | Extended: `.qwen/worktrees/`, `OTHER-TEAMMATE-STUFF/`, `.DS_Store`, `.helios-run/`, `engine.log`, `data/core/`. |

Superseded by the above: the per-package `PYTHONPATH=src` incantations and
`platform/agent-integration/.venv`. All documented commands repo-wide now use
`.venv/bin/python` from the repo root.

### 3.2 The deployment engine — `platform/deployment/`

The largest single addition, and the one other worktrees are most likely to build
against. Imported from a teammate's Mac-mini build, 20 source files, layout
preserved as upstream had it.

**What it is.** The deployment contract every app adopts: a `helios.yaml` manifest,
an ASGI entry point, and `GET /health`. Three importable pieces:

| Module | Role |
|---|---|
| `engine.contracts` | `load_manifest` (defaults, name sanitisation, port range, permission allow-list) and `lint` (the static secret scanner) |
| `engine.orchestrator` | The whole Docker lifecycle via docker-py: golden-image build, sandbox smoke test with crash-log capture, per-app audit sidecar, tenant network, nginx routing |
| `engine.app` | FastAPI ingest endpoint — upload a bundle, get it built and routed |

Plus `cli.helios` (talks to a running engine over HTTP), `helios_sdk` (what a
generated app imports: `propose_action`, `publish_event`, `subscribe_events`),
three golden-image Dockerfiles with their sidecar servers, a sample app bundle, and
`bootstrap.sh` / `test-deploy.sh`.

**The pipeline it implements**, which is what makes it more than a docker wrapper:
ingest → static secret lint → fast build on a prebuilt golden base (a single `COPY`
layer, so deploys take seconds and never reinstall dependencies) → sandbox smoke
test that polls `/health` and returns the crash log to the caller → attach a
per-app audit sidecar on an internal tenant network → generate nginx config and
reload.

**Security properties its tests pin**, since these are the ones a refactor could
silently break:

- The tenant execution secret appears in neither the injected Dockerfile nor any
  nginx config, and never reaches an app or smoke container — it goes only to the
  audit sidecar. That split is the entire reason the sidecar exists.
- The tenant network is created `internal=True`; the ingress network deliberately
  is not. Getting those backwards either exposes tenant traffic or makes every app
  unreachable.
- Tenant state is created once and reused, so a redeploy cannot orphan a running
  sidecar by rotating the secret.
- All writes go under `.helios-run/`; a test asserts nothing lands outside it.

**Three adaptations, all on the engine side** because the app side cannot absorb
them: `bootstrap.sh` now detects the Docker socket (`$DOCKER_HOST`, then OrbStack,
Colima, `/var/run/docker.sock`) instead of assuming OrbStack, builds its venv on
`python3.13`, and prefers the shared repo venv when present; the stale Traefik
lines were replaced (the script starts nginx — there is no Traefik anywhere in the
engine); a new `pyproject.toml` maps `sdk/helios_sdk` to flat `helios_sdk`, which
is the documented import name and the one the golden base image installs.
`requirements.txt` is pinned to match `requirements-dev.txt`.

**96 tests, all offline** — docker-py mocked with `unittest.mock`, no daemon, no
network, no credentials. `REPO` and `TENANTS_DIR` are redirected to a temp dir so
no test writes tenant state into the working tree. The smoke-test poll is
monkeypatched, otherwise a failing-smoke case would burn 45 real seconds.

**Excluded from the import, deliberately:** `.venv/`, `__pycache__/`, `.DS_Store`,
`engine.log`, and the whole `.helios-run/` directory. `.helios-run/tenants/demo.json`
holds a live tenant execution secret and was never copied — verified absent from
both the copied tree and every staged file list.

**Unexercised:** it has never touched a real Docker daemon. `bootstrap.sh` and
`test-deploy.sh` are unverified end to end, and no app in this repo has been
deployed through it. See §6.

### 3.3 New financial-tool packages

Four previously untracked teammate packages, imported as siblings of `fin_tools`
under one shared `pyproject.toml`:

| Package | Source | Tests |
|---|---|---|
| `helios_forecast` | folder copy (newer than `origin/forecasting-simulation`) | +68 |
| `spreadsheets` | `origin/payments` — that branch is the spreadsheets tool despite its name | +67 |
| `email_tool` | `OTHER-TEAMMATE-STUFF/email-tool/sender/` | +105 |
| `payments_collect` | `OTHER-TEAMMATE-STUFF/payments-tool/collect/` | +38 |

`platform/financial-tools/` now holds **five sibling packages installed by one
pyproject**. All four suites run offline with zero skips — verified rather than
assumed. Notably the spreadsheets "live" engines (Feishu, WPS, Grist) are served
in-process by a `LocalSheetEngine` that only formats URL strings, so a test
rendering to `target="feishu"` never opens a socket; and Qwen inference is injected
as `NullQwen`.

Import mechanics worth knowing if another worktree adds a sixth package: the
pyproject uses an **explicit** `[tool.setuptools] packages` list (flat-layout
auto-discovery refuses `fin_tools` because of its non-package siblings), so a new
package must be added there **and** to `bootstrap-venv.sh`'s `pip install -e` list,
or it will not install.

### 3.4 Six defects fixed

Each has a test observed to fail before the fix and pass after.

1. `auth_code` / `authorization_code` added to `audit_agent`'s `REDACTED_KEYS`. A
   payment credential was reaching the hash-chained log in full via `owner_question`.
2. `RunResult.summary()` counted read-only calls as side effects. New
   `side_effects` property filters on `proposal_id`; `executed` redefined over it.
3. Contact recording was hard-coded to `name == "send_email"`. Now driven by an
   optional `records_contact: bool = False` on `ToolDefinition`.
4. Model driver re-targeted from a legacy vendor to **Qwen via DashScope**.
5. `check_app.py` documented a side-effect-flag cross-check it never performed.
   Now implemented, with its own suite at `entry-point/checks/tests/`.
6. `load_csv` read with `utf-8`, leaving a BOM attached to the first header
   (`invoice_id` → `\ufeffinvoice_id`). Now `utf-8-sig`; fixture BOM stripped.

Two further real defects were found and fixed while testing, beyond the brief:

- `platform/deployment/engine/orchestrator.py` `_build_error_text` read
  `exc.build_logs`; docker-py sets `build_log` (singular). The `getattr(..., [])`
  default made it silent, so **every build failure dropped its compiler output**.
- The `.env` linter gap described in §6 below.

### 3.5 Decisions and docs

Six decisions recorded as decided in `specs/decisions.md` (each with a one-line
why); `specs/tech-stack.md` synced; `specs/architecture-mermaid.md` **prose**
updated to list `platform/agent-integration` and `platform/deployment` as real
components; `agent_toolkit.audit.AuditGate` marked deprecated in its docstring;
13 stale docs corrected; `QWEN.md` added at repo root (exactly 80 lines).

---

## 4. Verification state

```
scripts/test-all.sh  →  all 7 suites green: 803 passed
```

| Suite | Passed |
|---|---|
| `platform/financial-tools/tests` | 367 |
| `platform/agent-integration/tests` | 136 |
| `platform/deployment/tests` | 96 |
| `platform/audit-agent-builder/tests` | 81 |
| `apps/helios-collect/tests` | 74 |
| `entry-point/examples/invoice-payer/tests` | 41 |
| `entry-point/checks/tests` | 8 |

Baseline before this session was **403 across 5 suites**; that was re-measured and
matched before anything was changed. Everything is offline: no network, Docker
daemon, model token, or provider credential.

The venv was verified twice — once built by hand, then deleted and rebuilt through
`bootstrap-venv.sh` (exit 0, all packages import, suite green).

**These counts predate the two new `chloe-dev` commits** described in §1, which add
43 tests in `platform/financial-tools/tests/test_market_data.py`. That file is not
in this branch, so `financial-tools` shows 367 here and should show ~410 after a
merge. Those new tests were inspected and appear offline (injected fakes,
monkeypatched token, no live calls), but **they have not been run in this worktree**
— nobody should treat 803 as the post-merge total.

One merge-relevant fact about them: `fin_tools/market_data.py` is stdlib-only
(`urllib`, `json`, `os`, `socket`, `dataclasses`) and the base commits touched no
`pyproject.toml` or requirements file, so **`requirements-dev.txt` needs no new
pins** to accommodate them.

---

## 5. Interface and contract changes

**These are what a merge is most likely to break, or to be broken by.**

### Breaking

| Change | Effect on other work |
|---|---|
| `ModelDriver.__init__` **no longer accepts `reasoning_effort`** | Any code constructing `ModelDriver(reasoning_effort=...)` raises `TypeError`. `cli.py`'s `doctor` did and was fixed here. |
| `TOKEN_ENV_VARS` is now `(HELIOS_LLM_API_KEY, DASHSCOPE_API_KEY, OPENAI_API_KEY)` | `LLM_API_TOKEN`, `GLM_API_KEY`, `ZHIPUAI_API_KEY` are **no longer read**. A worktree relying on them will silently find no token. |
| `DEFAULT_BASE_URL` / `DEFAULT_MODEL` changed | DashScope + `qwen3.8-max`. Any test asserting the old constants fails. |
| `agent-toolkit` console script removed from `action-agent-builder/pyproject.toml` | It pointed at a non-existent `agent_toolkit.cli:main` and made `pip install -e` fail. Anything invoking an `agent-toolkit` CLI breaks. P3 is expected to write `cli.py` and restore the entry. |
| `RunResult.executed` semantics narrowed | Now side effects only; previously included successful read-only calls. |
| `REDACTED_KEYS` grew | Redaction happens at request **ingestion**, before schema validation. A manifest putting `minLength`/`pattern` on `auth_code` now validates only the 14-char `***REDACTED***` and rejects every payment. `invoice-payer`'s manifest dropped its `minLength: 16` for this reason. **Any worktree adding a length/format constraint on a credential-shaped argument will hit this.** |

### Additive (backward compatible)

- `ToolDefinition.records_contact: bool = False` — new optional field, appended
  after `handler` with a default, so keyword and 6-arg positional construction
  both still work. Validated as a bool in `__post_init__`.
- `RunResult.side_effects` property.
- `model_driver` exports `BASE_URL_ENV_VARS` and honours `OPENAI_MODEL` /
  `OPENAI_BASE_URL` as fallbacks.
- `check_app.py` internals `_action_side_flags()` and
  `_check_side_effect_agreement()`.

### Packaging

- `platform/financial-tools/pyproject.toml` switched to setuptools with an
  **explicit** `[tool.setuptools] packages` list, because `fin_tools` is
  flat-layout with non-package siblings and auto-discovery refuses it. **Any new
  sibling package must be added to that list or it will not install.** Current
  list: `fin_tools`, `helios_forecast`, `spreadsheets`, `spreadsheets.dev`,
  `email_tool`, `payments_collect`.
- `scripts/bootstrap-venv.sh` installs five packages; a sixth first-party package
  must be added to its `pip install -e` list.
- `platform/deployment/pyproject.toml` maps `sdk/helios_sdk` → flat `helios_sdk`
  (the documented name, and what the golden base image installs).

### Observable behaviour changes

- `check_app.py apps/helios-collect` now reports **4 warnings, not 3**. The new
  cross-check cannot resolve three app-local tools into a platform catalog. All
  five of that app's flags were verified by hand to match its audit manifest, so
  the warning is informational. **Any worktree or test asserting "3 warnings"
  breaks.** `invoice-payer` still reports 0 errors / 3 warnings.
- `platform/financial-tools/fixtures/invoices.csv` is now BOM-free (489 bytes,
  was 492; byte-exact `old == BOM + new`).

---

## 6. Known gaps, deliberate non-work, and unverified claims

Recorded so nobody assumes more was done than was.

- **The live model path is UNVERIFIED.** No model key exists in this environment,
  so `agent_integration.cli doctor`'s `OK` branch was never observed — only its
  no-key path (exits 1, names the three accepted variables). Nobody should read
  the Qwen re-target as proven against a real endpoint. Run `doctor` with a real
  key before claiming it.
- **The deployment engine has never touched a real Docker daemon.** Its 96 tests
  mock docker-py and redirect `REPO`/`TENANTS_DIR` to a temp dir. `bootstrap.sh`
  and `test-deploy.sh` are unexercised end to end. No app in this repo has been
  deployed through it.
- **A real linter gap was found and deliberately NOT fixed.**
  `engine/contracts.py` `lint()` matches `TEXT_EXT` against `Path.suffix`, and
  `Path(".env").suffix` is `""` — so a file named exactly `.env` is **never
  scanned**, while `secrets.env` is. Pinned by a test so nobody assumes `.env` is
  covered. Left alone because P0 does not change teammate behaviour. Actual
  protection today is that `.env` is gitignored.
- **No scaffolding, validation-model or UI-generator work was done here, by
  design.** This session's scope was foundation only: environment, imports,
  defects, docs. `platform/app-builder/` is untouched except for one prose fix to
  its `PLAN.md` (a dangling reference to a deleted `TODO.md`), and
  `specs/app-builder.md` was not edited at all. The one validator-adjacent change
  is `entry-point/checks/check_app.py`, which gained the side-effect cross-check it
  had always documented but never performed (§3.4, defect 5) plus its own test
  suite. That script is a stand-in, not the real validator, and says so in its own
  docstring.
- **Holds are still in-memory only** in the audit builder; `InMemoryReplayGuard`
  is single-process. `specs/decisions.md` records that held state and replay
  guards persist to SQLite under the app's data dir — **P2 implements that.**
- No web UI anywhere; every app is CLI-only. No database; JSON fixtures in memory.
- `pandas` and `rapidfuzz` are declared as an optional `import` extra on
  `fin-tools` but nothing imports them; kept so the upstream intent is recorded
  rather than silently dropped.
- `tzdata` is a **hard** dependency of `payments_collect` — `models.py` resolves
  `ZoneInfo("Asia/Shanghai")` at import time, so the IANA database must exist or
  the package fails to import.

---

## 7. Collision surface

**42 pre-existing files were modified.** The ones most likely to also be touched
by a parallel worktree:

Highest risk (shared infrastructure, whole-file or structural edits):

- `.gitignore`
- `requirements-dev.txt` *(new, but every worktree needs deps)*
- `scripts/bootstrap-venv.sh`, `scripts/test-all.sh` *(new)*
- `HANDOFF.md` — §2, §4 and §7 were rewritten; §6's headline open questions marked
  resolved
- `specs/decisions.md`, `specs/tech-stack.md`, `specs/architecture-mermaid.md`
- `SETUP.md` — was one line, fully rewritten
- `platform/README.md`
- `platform/financial-tools/pyproject.toml` — build system **and** package list
- `platform/action-agent-builder/pyproject.toml` — dep and script entry removed

Contract code (small, targeted, but semantically load-bearing):

- `platform/action-agent-builder/src/agent_toolkit/contracts.py`
- `platform/action-agent-builder/src/agent_toolkit/tools.py`
- `platform/action-agent-builder/src/agent_toolkit/audit.py` *(docstring only)*
- `platform/audit-agent-builder/src/audit_agent/contracts.py`
- `platform/agent-integration/src/agent_integration/model_driver.py`
- `platform/agent-integration/src/agent_integration/runtime.py`
- `platform/agent-integration/src/agent_integration/cli.py`
- `platform/financial-tools/fin_tools/spreadsheet_tools.py`
- `entry-point/checks/check_app.py`
- `entry-point/examples/invoice-payer/agents/invoice-payer-audit.yaml`

The remaining modified files are prose docs (`entry-point/SKILL.md`,
`entry-point/BUILD.md`, `entry-point/README.md`, three files under
`entry-point/references/`, four package/example READMEs,
`platform/financial-tools/PLAN.md`, `platform/app-builder/PLAN.md`) plus three
test files (`test_integration.py`, `test_model_driver.py`,
`tests/conftest.py` in agent-integration; `test_log_transport_manifest.py` in
audit-agent-builder; `test_controls.py` in invoice-payer;
`test_email_and_sheets.py` in financial-tools) and
`apps/helios-collect/{README.md,facts.py}`.

**92 files were added**, overwhelmingly under two new directories
(`platform/deployment/`, and the new sibling packages plus prefixed tests under
`platform/financial-tools/`) — low collision risk except for the shared files
listed above.

Test files added under `platform/financial-tools/tests/` are **prefixed by
package** (`test_forecast_*`, `test_email_*`, `test_payments_collect_*`) to avoid
collisions in that flat directory. `test_spreadsheets.py` is not prefixed — it
came from a branch that already used that name.

### Overlap with the two new `chloe-dev` commits

Checked file by file, because those commits landed after this branch diverged and
are the most likely source of a merge surprise:

| File touched by base | Touched by this branch? |
|---|---|
| `platform/financial-tools/fin_tools/service.py` | **No** |
| `platform/financial-tools/fin_tools/__init__.py` | **No** |
| `platform/financial-tools/fin_tools/market_data.py` *(new in base)* | **No** |
| `platform/financial-tools/tests/test_market_data.py` *(new in base)* | **No** |

**Zero file overlap.** A textual merge of these four files should be clean, and no
`pyproject.toml` or requirements conflict arises from them (base touched neither).

Two semantic interactions still worth a look after merging, neither of which is a
textual conflict:

- `fin_tools/__init__.py` is the module that exports `build_world()` and the tool
  count. This branch does not edit it, but `entry-point/references/platform-status.md`
  and the financial-tools README both state "12 registered tools". If
  `market_data.py` registers additional tools, **those two documented counts go
  stale on merge.** Verify with
  `.venv/bin/python -c "from fin_tools import build_world; print(len(build_world().registry.list()))"`.
- This branch added four sibling packages to the explicit
  `[tool.setuptools] packages` list in `platform/financial-tools/pyproject.toml`.
  Base did not touch that file, so no conflict — but any *further* package added by
  another worktree must also be added to that list or it will not install.

---

## 8. Rules this worktree operated under

Stated because a merge should preserve the properties they protect.

- No push, force-push, rebase, amend, reset, history rewrite, or `--no-verify`.
  Explicit paths staged; never `git add .` / `-A`.
- `OTHER-TEAMMATE-STUFF/`, `docs/milestones/`, `.env`, and every `.venv` were
  never modified. `OTHER-TEAMMATE-STUFF/` was read only.
- No control was weakened to make a test pass. Where the credential-redaction fix
  broke 17 example tests, the **fixtures** were corrected and the gate left
  intact; a test that literally asserted the old gap was open was inverted.
- Qwen is the only generative model allowed. Every reference to the previous
  vendor — endpoints, model ids, env-var names — was removed from every file this
  session touched. A final repo-wide grep returns nothing outside `.venv` and
  these session notes.

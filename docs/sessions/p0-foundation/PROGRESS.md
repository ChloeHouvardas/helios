# Progress — session `p0-foundation`

Three lines per entry: what changed, what was observed, what is next.

---

## 0. Session start, baseline recorded

Set up the worktree the prompt assumed existed but did not, then measured the
baseline before touching anything. Observed 403 passed across five suites, all
offline, via `PYTHONPATH` injection because no venv existed yet:
audit-agent-builder 79, financial-tools 86, agent-integration 123,
helios-collect 74, invoice-payer 41. Five deviations from the prompt are
recorded in PLAN.md; the material ones are that the worktree/branch had to be
created, `docs/research/` does not exist (Task 0 has nothing to commit), and
the prompt-set directory outside the repo is unreadable (EPERM), so the
truncated tail of the task prompt could not be recovered. Next: Task 1.

## 1. Task 1 — one environment, one test command

Fixed the two un-installable pyprojects first, since `pip install -e` fails
otherwise: `fin_tools` gained a setuptools `[build-system]` plus an explicit
`[tool.setuptools] packages = ["fin_tools"]` (automatic flat-layout discovery
refuses the non-package siblings `tests/`, `fixtures/`, `research/`);
`agent_toolkit` dropped the unused `qwen-agent>=0.0.17` declaration and the
`[project.scripts]` entry for `agent_toolkit.cli:main`, which pointed at a
`cli.py` that does not exist yet (P3 writes it). Wrote
`scripts/bootstrap-venv.sh`, `scripts/test-all.sh`, `requirements-dev.txt`
(every version pinned to what actually resolved), extended `.gitignore`
(`.qwen/worktrees/`, `OTHER-TEAMMATE-STUFF/`, `.DS_Store`, `*.egg-info/`,
`.helios-run/`, `engine.log`, `data/core/`), and replaced the per-package
`python3 -m pytest` / `PYTHONPATH=src` instructions in HANDOFF.md §4, SETUP.md
and five package/example READMEs with repo-root venv commands.

Verified two ways. The venv resolved pytest 9.1.1 and pydantic 2.14.1 — newer
than anything the baseline had seen — and all five suites were still green
inside it at 403. Then deleted `.venv` entirely and rebuilt through
`bootstrap-venv.sh`: exit 0, all four first-party packages import, and
`test-all.sh` printed `all 5 suites green: 403 passed`. Also confirmed the
`doctor` subcommand referenced by the new SETUP.md really exists
(`cli.py:378`) rather than documenting an imagined interface. Next: Task 2,
importing the deployment engine.

Note for later tasks: `qwen_agent` is legitimately imported at
`audit_agent/advisory.py:202` behind a lazy optional extra, so the
`audit-agent-builder` `advisory` extra stays. Only the `action-agent-builder`
declaration was dead.

## 2. Task 2 — deployment engine imported as platform/deployment

Copied 20 files from OTHER-TEAMMATE-STUFF/deployment-stuff keeping the upstream
layout, excluding `.venv/`, `.helios-run/` (the tenant secret `demo.json` lives
there — verified absent from the copy and from the staged set), `engine.log`,
`__pycache__/`, `.DS_Store`. Added `pyproject.toml` (`helios-deployment`, maps
`sdk/helios_sdk` to flat `helios_sdk` because that is the documented/base-image
import name; `cli/helios.py` does define `main()`, so the console script is
real), pinned `requirements.txt` to match `requirements-dev.txt`, and rewrote
`bootstrap.sh`: Docker socket now detected in the required order ($DOCKER_HOST,
OrbStack, Colima, /var/run/docker.sock) instead of hard-coded OrbStack, venv on
python3.13, and the stale Traefik lines replaced (the script starts nginx; there
is no Traefik anywhere in the engine). Wrote 96 offline tests (docker-py mocked
with `unittest.mock`, `REPO`/`TENANTS_DIR` redirected to a temp dir so no test
writes tenant state into the tree).

Fixed one real defect while testing: `_build_error_text` read `exc.build_logs`,
but docker-py sets `BuildError.build_log` (singular); the `getattr(..., [])`
default made the typo silent, so every build failure dropped the compiler
output and reported only the short reason — defeating the documented
crash-log-to-agent feedback. Now reads the correct attribute. Also recorded
(not changed) a real coverage gap: `lint()` matches `TEXT_EXT` against
`Path.suffix`, and `Path(".env").suffix` is `""`, so a bare `.env` is never
scanned; pinned by a test so nobody assumes it is. Verified `test-all.sh`:
`all 6 suites green: 499 passed` (403 + 96). Next: Task 3.

## 3. Task 3 — four teammate packages imported into financial-tools

All four imported as siblings of `fin_tools`, registered in the shared
`platform/financial-tools/pyproject.toml` explicit package list, installed from
the one repo venv. Each is its own commit. Test files moved under
`platform/financial-tools/tests/` with package-name prefixes; internal imports
were relative so no package code changed. New deps pinned in
requirements-dev.txt: numpy, openpyxl (+et-xmlfile), jinja2, jsonschema, tzdata.

- **helios_forecast** (+68): folder copy is NEWER than the branch (adds
  calibration.py, validation.py); CRLF normalised to LF. Also copied the two
  forecasting specs and financial_api/market.py (stdlib-only, so P1 can wire it).
  Skipped apps/forecast-demo, M1/M2 docs, and every committed venv/cache/egg-info.
- **spreadsheets** (+67): from `origin/payments` via checkout+reset (that branch
  is the spreadsheets tool despite the name). Renamed the developer guide to
  README.md. Offline verified, not assumed: the "live" feishu/wps/grist targets
  are served by LocalSheetEngine (in-process, formats URL strings, never dials)
  and Qwen is injected as NullQwen — zero skips, no credentials touched.
- **email_tool** (+105): from email-tool/sender/, renamed `sender`->`email_tool`,
  kept templates/ + schemas/ as package-data. Two relocation fixes:
  test_email_schemas.py now imports SCHEMAS_DIR from the package (its
  `Path(__file__).parent.parent/schemas` hop broke on the move), and the default
  idempotency data dir follows the package (rule unchanged). Corrected the
  Windows-only venv paths + dropped-conftest description in DEVELOPER-GUIDE.md.
- **payments_collect** (+38): from payments-tool/collect/, renamed
  `collect`->`payments_collect` to avoid a generic name in the flat layout.
  tzdata is a HARD dep: models.py resolves `ZoneInfo("Asia/Shanghai")` at import,
  so the IANA db must exist or the package won't import. No fastapi/uvicorn/httpx
  despite the upstream requirements listing them (a provider layer never written).

Every package checked for GLM/Zhipu/bigmodel references — none. All offline,
zero skips across all four. financial-tools grew 86 -> 364. Verified
`test-all.sh`: `all 6 suites green: 777 passed`. Next: Task 4, the six defects.

## 4. Task 4 — the six known defects

One commit each, each with a test observed to fail before and pass after. Total
suite went 777 -> 803.

1. **`auth_code` redaction** (779). Added `auth_code` / `authorization_code` to
   `REDACTED_KEYS`. The leak was real: the pre-fix test showed
   `auth_code=OTP-9f3c-LIVE-SECRET` rendered inside `owner_question`, which is
   written to the hash-chained log. This broke 17 invoice-payer tests, and the
   correct reading was that the *fixtures* were wrong, not the gate: the manifest
   had `auth_code: {minLength: 16}`, but redaction happens at ingestion, so the
   gate could only ever validate the 14-char `***REDACTED***`. Removed the
   constraint (presence is enforced by `required` + the critical
   `requires_verified_intent` rule, validity by the tool at execution, which
   receives the real argument). Also inverted a test that literally asserted
   `"auth_code" not in REDACTED_KEYS  # the upstream gap is still open`.
2. **`summary()` side-effect count** (781). Read-only calls are appended to
   `actions` too, so a lookup-plus-send run reported 2 and 2. Added a
   `side_effects` property filtering on `proposal_id` (set exactly when a call is
   adjudicated) and redefined `executed` over it.
3. **Contact recording generalised** (786). Replaced the hard-coded
   `name == "send_email"` with an optional `records_contact: bool = False` on
   `ToolDefinition`, set on `make_send_email`. helios-collect reuses that factory
   as-is, so it inherits the marker — no duplicate marker in the app. Its 74
   tests, including the contact-frequency controls, are the real regression guard.
4. **Model driver re-targeted to Qwen** (792). Defaults now DashScope +
   `qwen3.8-max`; token order `HELIOS_LLM_API_KEY, DASHSCOPE_API_KEY,
   OPENAI_API_KEY`; `enable_thinking: false` only for `qwen3` ids, neither
   thinking field otherwise. The mock now *enforces* that contract in both
   directions. `cli.py` passed `reasoning_effort="low"` to `doctor`, which would
   have raised TypeError on every run. **Unverified:** no model key exists in this
   environment, so `doctor`'s live `OK` branch was never observed — only its
   no-key path (exits 1, names the three variables). Recorded rather than claimed.
5. **`check_app.py` cross-check implemented** (800). The docstring promised a
   side-effect-flag cross-check no code performed. The action manifest carries
   names only, so flags are resolved from the catalogs the runtime actually gates
   on (`agent_toolkit`, `fin_tools`). Kept side-effect free — `register()` on a
   fresh registry, never `bind()`, never the global `REGISTRY`; asserted by test.
   The direction assertions caught an inverted branch during development: the
   consequence message must key off the action side's flag.
6. **CSV BOM** (803). `load_csv` read with `utf-8`, leaving the BOM attached to
   the first header, so `invoice_id` arrived as `\ufeffinvoice_id`. Now `utf-8-sig`;
   the fixture's BOM stripped (byte-exact: `old == BOM + new`, 492 -> 489).
   Survived because every existing test read `amount_cents` or `customer`, never
   the leading column.

Also found while testing Task 2 and fixed there: the deployment engine's
`_build_error_text` read `exc.build_logs` but docker-py sets `build_log`, so every
build failure silently dropped its compiler output.

## 5. Tasks 5-7 — decisions, stale docs, QWEN.md

Recorded six decisions as decided in `specs/decisions.md` (each with a one-line
why), synced `specs/tech-stack.md`, listed `platform/agent-integration` and
`platform/deployment` as real components in `specs/architecture-mermaid.md` prose,
and marked `agent_toolkit.audit.AuditGate` deprecated in its docstring — verified
nothing instantiates it anywhere, so the legacy marking is accurate.

Corrected the stale docs. Note `check_app.py` on helios-collect now reports 4
warnings, not 3: the new cross-check cannot resolve three app-local tools. All
five of that app's flags were verified by hand to match its audit manifest, so the
warning is informational; documented in its README rather than suppressed.

`QWEN.md` written at the repo root, exactly 80 lines, six sections, all seven
decisions.

**Process note:** Task 6 was first delegated to a fork, which reported 12 files
edited with per-file summaries. `git status` was empty in both the worktree and
the main checkout — nothing had been written. The report was fabricated; all 12
files were edited directly instead. Lesson applied: verify a sub-agent's claimed
edits against the filesystem before trusting them.

Final: `scripts/test-all.sh` reports `all 7 suites green: 803 passed`.

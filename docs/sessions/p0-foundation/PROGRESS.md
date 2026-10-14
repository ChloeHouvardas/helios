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

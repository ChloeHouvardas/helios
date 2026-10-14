# p3-builder — progress log

Three lines per entry: what changed, why, what it affects.

## 0. Environment bootstrap

- Created the worktree and branch (neither existed), wrote `scripts/bootstrap-venv.sh`
  and `scripts/test-all.sh` (P0 had not landed them), and built `.venv` from
  `/opt/anaconda3/bin/python3.13`.
- Why: without them there is no interpreter with the dependencies and no way to
  record a baseline, and section D requires both before any edit.
- Affects: every later task. `scripts/` is a shared path — P0 must reconcile rather
  than overwrite; noted in `HANDOFF-TO-P1-P2.md`.

## Baseline

`scripts/test-all.sh` did not exist, so no baseline could be taken before writing it.
The counts below were taken immediately after, before any existing file was modified
— which for this session is the same thing, since Task 1 only adds files.

`scripts/test-all.sh` exits 0. Counts are recorded per suite so a later regression is
attributable to a named package rather than to "something broke".

| suite | result |
|---|---|
| audit-agent-builder | 79 passed |
| agent-integration | 123 passed |
| financial-tools | 86 passed |
| app-builder | 57 passed (new this session) |
| helios-collect | 74 passed |
| action-agent-builder | skipped — no `tests/` directory |
| entry-point | skipped — no `tests/` directory |

Pre-existing total excluding app-builder: **362 passed**.

One bug found and fixed while establishing this baseline: four packages declare
`addopts = "-q"` in their own `pyproject.toml`, and the script passed `-q` too. The
two combine to `-qq`, which suppresses pytest's "N passed" summary — so the script
reported green with no counts at all, which is the worst of both outcomes.
`test-all.sh` now clears per-package `addopts` with `--override-ini="addopts="` and
applies exactly one `-q` itself, so the counts do not depend on each package's own
configuration.

## 1. Task 1 — package and spec model

- Added `platform/app-builder/{pyproject.toml,src/app_builder/{__init__,spec}.py,tests/test_spec.py}`.
  `spec.py` is the pydantic v2 model of `helios-app.yaml`: `extra="forbid"` everywhere,
  closed enums for ui patterns and field types, `stack.use` xor `stack.match_existing`,
  `data.local` xor `data.integration`, kebab `identity.id`, and `AppSpecError` carrying
  every problem at once as `<yaml path>: <problem>; write <what instead>`.
- Why: the spec is the contract the whole builder reads. Shape validation is kept
  separate from cross-file validation so a malformed spec is rejectable without
  touching the filesystem, and so `validate.py` can assume the shape is sound.
- Affects: nothing yet — no existing file was modified. Both real apps' specs load
  unchanged; 57 new tests pass.

### Two safety properties added beyond the prompt

1. **SQL identifier validation.** Entity and field names are interpolated into
   generated DDL by Task 3, so `spec.py` refuses anything outside
   `^[a-z_][a-z0-9_]*$`. Refusing is not the same as escaping: an escaped name would
   still let a spec author create an arbitrarily-named column, and the escaping would
   have to be correct in every generated statement forever.
2. **Money heuristic excludes `string`.** Fixes the `payer_behavior` false positive
   named in Task 2. The heuristic now only fires where a float would genuinely be a
   bug, because a checker that cries wolf on prose fields gets ignored — and an
   ignored checker misses the real one.

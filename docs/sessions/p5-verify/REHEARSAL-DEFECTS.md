# Surprise-task rehearsal — defects found

Rehearsal of Task 4, run 2026-10-15 in the throwaway clone `/tmp/helios-t4` on
`worktree-p3-builder` @ `c742d0b`. The rehearsal agent was given only
`entry-point/SKILL.md` and `entry-point/BUILD.md` and the sentence:

> "Build an app that approves employee expense claims against a monthly budget per
> department and reimburses the approved ones, using Helios."

It was explicitly forbidden from reading `platform/app-builder/` source to work out what a
command does. Everything below was observed in tool output.

## Result

The app reached **validator 0 errors** and **16/16 control tests passing**
(`budget_ceiling_hold`, `duplicate_claim_refused`,
`reimbursement_requires_owner_intent`, `lying_agent_caught`), and `/health` returned
**200**. It could **not** be deployed through the documented path.

Verdict: a fresh agent given only the two docs **could not** complete this task without
significant guessing. Two blockers, below.

## Blockers

**1. The spec `tools:` format is undocumented.** `BUILD.md` gives no example of what goes
in `tools:`. Full `ToolDefinition` objects produce `Input should be a valid string`; the
correct form is plain string names, discoverable only by reading another app's
`helios-app.yaml` — which the rehearsal rules forbid. **Fix:** show
`tools: [claims_lookup, budgets_remaining]` in BUILD.md step 2 or 3.

**2. The documented deploy path does not exist.** `BUILD.md:358` says to run
`cli/helios.py deploy`. There is no such file relative to an app, and `helios-app` has no
`deploy` subcommand. The real CLI is `platform/deployment/cli/helios.py`, installed as
`helios`, and it needs a `helios.yaml` the scaffolder does not generate. **Fix:** either
add `helios-app deploy` that writes `helios.yaml` and calls the engine, or document the
two-step path.

**3. The scaffolder emits an app its own validator rejects.** `helios-app new` writes
`mandate.allowed_tools: []` into the action manifest; `helios-app validate` then fails
with `mandate.allowed_tools must be a non-empty list`. Every freshly scaffolded app is
born invalid. **Fix:** copy the spec's tool list into `mandate.allowed_tools`.

## Major

**4. Side-effect detection is name-blind.** A tool named `reimbursements_send` was
generated with `side_effect: false` in both the audit manifest and `tools.py`. On this
platform that is not a cosmetic error — an ungated side effect is the exact failure the
architecture exists to prevent. **Fix:** improve the heuristic, or require explicit
`side_effect` annotation per tool in the spec.

**5. Generated `tools.py` is a stub.** All tools get `input_schema.properties: {}` and
generic TODO descriptions, because the spec carries no schemas. The agent must rewrite the
file entirely, which the docs do not say. **Fix:** let the spec carry tool schemas, or
state plainly that `tools.py` must be rewritten.

**6. Side-effect tools need `input_schema` in the audit manifest, and BUILD.md step 5
never says so.** The first indication is the validator error
`tool has side effects but declares no input_schema`. **Fix:** document it in step 5.

**7. `bootstrap-venv.sh` omits `fastapi` and `uvicorn`,** but the generated `server.py`
imports them, so a scaffolded app cannot boot from a cold clone without a manual
`pip install`. **Fix:** add both to the pinned dev requirements.

## Minor

**8. `BUILD.md:129` claims 23 files are generated; 20 were written.**

**9. Generated `test_controls.py` sets `GATED_TOOLS = []`,** so the control tests skip
silently rather than failing — an app can look tested while nothing is asserted. **Fix:**
populate it from the spec's `side_effect: true` tools.

## Environment limitation (not a defect)

Docker Hub (`registry-1.docker.io`) is unreachable from this machine, so a containerised
deploy could not be exercised here. The engine itself was verified separately in
`/tmp/helios-pre` using mirror-pulled images — see `BLOCKED.md` §Task 2 and `STATUS.md`
§"How to demo in 10 minutes".

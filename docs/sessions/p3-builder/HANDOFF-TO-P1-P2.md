# Requests from p3-builder to P1 and P2

Left here rather than sent, because nobody is reading during the session and the
changes are all optional or already worked around. Nothing in this file blocks
p3-builder; every item has a local workaround that is recorded where it matters.

## To P0 (foundation) — `scripts/` is a shared path

`scripts/` did not exist in any worktree when this session started, and section D
requires `scripts/bootstrap-venv.sh` and `scripts/test-all.sh` before any edit. Both
were written here. **They are almost certainly going to collide with P0's version.**

What p3-builder's `test-all.sh` does that a naive version does not:

1. Passes `--override-ini="addopts="` to pytest. Four packages declare
   `addopts = "-q"` in their own `pyproject.toml`; adding `-q` on the command line
   makes it `-qq`, which suppresses the "N passed" summary entirely. The suite then
   reports green with no counts, and the per-suite numbers the acceptance criteria
   ask for do not exist.
2. Discovers suites by convention (`<root>/tests` with `test_*.py`) instead of a
   hand-maintained list, so a package added by another session is picked up.
3. `cd`s into each suite root before running. `apps/helios-collect` does
   `import config` and `from book import Book` — app-local imports that only resolve
   when the app directory is the working directory.
4. Treats a suite with no `tests/` as skipped, not failed. `action-agent-builder` and
   `entry-point` are both in that state today.

Reconcile rather than overwrite; if P0's version already handles these, take that one.

## To P1 (`platform/financial-tools/`)

**1. The package cannot be installed with `pip install -e`.**

It is a flat layout (`fin_tools/`, `tests/`, `fixtures/` as siblings) with no
`[build-system]` section, so setuptools package discovery fails:

```
ERROR: Failed to build '.../platform/financial-tools' when getting requirements to
build editable
```

Workaround used here (venv-local, nothing in the repo touched): a `.pth` file in
`.venv/lib/python3.13/site-packages/` pointing at `platform/financial-tools`. That is
exactly what an editable install would have produced, and `scripts/bootstrap-venv.sh`
now does it automatically.

To fix properly, add to its `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
packages = ["fin_tools"]
```

**2. The knowledge-graph API is not what the task description said.**

There is no `publish_verified`. The real methods on `KnowledgeGraph`
(`fin_tools/knowledge_graph.py`) are:

```python
upsert_verified(self, kind, key, attrs, *, source) -> dict   # returns {"node_id", ...}
add_observation(self, kind, key, attrs, *, observer) -> dict # raises on verified node
```

`apps/helios-collect/tools.py:265` and `:340` use `upsert_verified`. p3-builder's
scaffolder generates `graph.py` against those real names. If P1 renames them, the
generated apps break — worth a note in whichever document settles the graph
interface.

**3. `ToolDefinition` has no `records_contact` field.**

Task description said P0 would add an optional one. The actual frozen dataclass
(`agent_toolkit/contracts.py`) is exactly:

```python
name, description, input_schema, output_schema, side_effect, handler
```

`output_schema` is required and non-empty. p3-builder generates tools with an explicit
`output_schema` and does not pass `records_contact`. If that field lands later, the
generated `tools.py` needs one extra kwarg on email-shaped tools.

## To P2 (`platform/deployment/`)

**1. `platform/deployment/` does not exist.**

The engine is only at
`OTHER-TEAMMATE-STUFF/deployment-stuff/fPT9INmvWJ/default/helios/`, which is
read-only. Task 5 says to read `platform/deployment/engine/contracts.py` "for the
current field set at generation time" — that path is unreadable until P2 lands it.

Consequence for the scaffolder: `helios.yaml` generation is written against the
`OTHER-TEAMMATE-STUFF` copy, and is **field-conservative**. When P2 lands
`platform/deployment/`, re-check `contracts.py` and widen the emitted fields.

**2. The deployment manifest accepts five fields, not eight.**

```python
class Manifest(BaseModel):
    name: str
    tenant: str = ""
    entry: str = "main:app"
    port: int = 3000
    permissions: list[str] = []
```

There is no `model_config = ConfigDict(extra="forbid")`, so pydantic's default
`extra="ignore"` applies: unknown keys are **silently dropped**. The task description
asks for `audit_manifest`, `data_dir` and an `env` list. Emitting them would produce a
`helios.yaml` that looks configured and is not — the fields vanish without an error,
which is the worst possible failure mode for a deployment contract.

p3-builder emits only the five accepted fields. If P2 adds `extra="forbid"` (worth
doing — a silently-dropped `data_dir` is how an app ends up writing to the wrong
place), the generated manifest is already compliant.

**3. `ALLOWED_TOOLS` is a closed set of four.**

```python
ALLOWED_TOOLS = {"payments", "email", "knowledge-graph", "events"}
```

`load_manifest` raises `ContractError` on anything else. The task description lists
eight families (`forecast`, `market-data`, `spreadsheets`, `collect` among them).
p3-builder maps tool families onto the four real values and drops the rest, so a
generated app cannot fail to deploy on an unknown permission. If P2 widens
`ALLOWED_TOOLS`, the mapping in `app_builder/scaffold.py` (`_PERMISSION_BY_FAMILY`)
should be widened with it — it is the single place that needs changing.

**4. `apps/helios-collect/` has no `server.py`, `templates/` or `helios.yaml` yet.**

Task 4 says to start from P2's templates if they have landed
(`git log chloe-dev -- apps/helios-collect/templates`). They have not, so the kit is
written from scratch under `app_builder/kit/`. When P2's templates land, the kit and
theirs should be compared — divergence in structure is the thing to avoid, since the
scaffolder copies kit templates into generated apps.

**5. No console script for `helios`.**

The CLI is invoked directly: `.venv/bin/python cli/helios.py deploy <dir> [--tenant X]`.
There is no `pyproject.toml` in that tree at all (a `requirements.txt` instead), so no
`helios` entry point exists. Task 8's BUILD.md step 9 therefore documents the
`cli/helios.py` form rather than a bare `helios deploy`. If P2 packages it, update
BUILD.md.

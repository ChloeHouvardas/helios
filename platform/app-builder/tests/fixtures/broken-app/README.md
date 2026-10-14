# broken-app — a fixture that is wrong on purpose

Consumed by `tests/test_validate.py`. `helios-app validate` on this directory must
exit 1 with at least eight distinct errors.

**Do not "fix" this app.** Every defect below is load-bearing: it is the only thing
that proves the corresponding check runs. If a check stops firing, the error count
drops and `test_validate.py` fails on the row that names it.

## Why the spec itself is valid

`validate_app` stops after shape errors, because a malformed spec makes every
cross-file check meaningless — the sections they read may be absent or the wrong
type. So if this fixture's `helios-app.yaml` were broken too, the validator would
report the shape problems, skip the cross-file checks entirely, and still look
healthy. `test_the_broken_fixture_spec_is_itself_valid` pins that precondition.

The same reasoning applies to the audit manifest: `AuditManifest.load()` raises on
an undeclared task/rule tool and on a side-effecting tool with no `input_schema`, so
a manifest that failed to load would collapse every cross-file check into one
"is invalid" error. It is written to load.

## Errors (8)

| # | Check | Defect | Where |
|---|---|---|---|
| 1 | action tools ⊆ audit declared | `undeclared_tool` is offered but never declared | action manifest |
| 2 | spec `tools` ⊇ both manifests | six tools missing from the spec's list | `helios-app.yaml` |
| 3 | `identity.id` == audit `app_id` | `broken-app` vs `not-broken-app` | both |
| 4 | some task allows something the agent offers | the only task allows none of them | audit manifest |
| 5 | rule arguments exist in `input_schema` | `required_arguments` names `ghost_argument` | audit manifest |
| 6 | rule facts are produced by the fact source | `max_from_fact: invoice_amount_cents`, never returned | audit manifest + `facts.py` |
| 7 | `side_effect` agreement (unsafe direction) | `misflagged_write` true in code, false in manifest | `tools.py` vs audit |
| 8 | `side_effect` agreement (safe direction) | `misflagged_read` false in code, true in manifest | `tools.py` vs audit |

Errors 7 and 8 are the same check in both directions, and the messages must not read
alike: #7 is a control bypass (a real side effect runs with no proposal, no hold and
no owner), #8 is a nuisance (a read is gated). `test_validate.py` asserts that the
"ungated side effect" wording appears only in #7.

## Warnings (8)

| # | Check | Defect |
|---|---|---|
| 1 | entity declares fields | `marker` has none, so its table holds only ids |
| 2 | integer money named `*_cents` | `invoice.total` is integer money, not `total_cents` |
| 3 | an `approval_queue` page exists | none declared, so holds are invisible |
| 4 | published entity is declared locally | `graph.publishes` names `ghost` |
| 5 | publishes without reads | declares publishes, no reads |
| 6 | action name == audit `app_id` | `broken-app` vs `not-broken-app` |
| 7 | side-effect tool has a rule | `ungated_write` has none |
| 8 | gated tool has a subject key | `send_notice` takes `to` and `body_length` only |

## Why the fact check reads the AST instead of calling the fact source

The obvious implementation — import `facts.py`, call `extra_facts_for("dummy")`,
compare keys — reports errors against a **correct** app. A fact source must return
`None`/`{}` for a subject it does not know; that is required, because unknown must be
None and never zero, or a fail-closed control becomes fail-open. So a dummy subject
yields no keys, every key looks missing, and `helios-collect` fails validation.

`probe_fact_keys` therefore reads the key set from the AST (a superset of what any
one call can return, which is the right question: "could this key ever be produced")
and still imports the module in a subprocess, to prove it imports and to resolve
constant names. `facts.py` here supplies four keys and omits one, so the check has to
discriminate — an empty fact source would make it trivially true.

`tools.py` is scanned the same way, because real apps build `ToolDefinition`s inside
factory functions that need a store and a bundle, so nothing can be enumerated from
module globals without constructing the whole app.

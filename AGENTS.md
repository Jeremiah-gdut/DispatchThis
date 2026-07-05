## Agent skills

### Issue tracker

Issues live in GitHub Issues; external PRs are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the default mattpocock/skills triage label vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context repo: root `CONTEXT.md` plus `docs/adr/`. See `docs/agents/domain.md`.

## DispatchThis workflow notes

Workflow callbacks are the orchestration boundary. Pass modules may build plans
and rewrite the current IL, but Binary Ninja reanalysis-triggering edits belong
in `plugins/DispatchThis/workflow.py`.

Treat these APIs as reanalysis triggers:

- `Function.set_user_indirect_branches`
- `Function.set_call_type_adjustment`
- `BinaryView.add_analysis_completion_event`

Do not call those APIs from low/medium pass modules. Repeating the same mutation
on every workflow run can keep function analysis looping.

Use `Function.session_data["dispatchthis_workflow_state"]` for function-scoped
phase state. Do not use `BinaryView.session_data` for indirect branch/call receipts.
`BinaryView.session_data` is only appropriate for view-level timing/state such as
the pending tag-cleanup completion callback.

Current phase order constraints:

- Indirect branch resolving must stabilize before indirect call resolving.
- Branch condition translation runs only after indirect branch resolving is
  stable.
- Branch-target cleanup runs after branch condition translation.
- Call-target cleanup runs after indirect call resolving is stable and no new
  call type adjustment was submitted in that run.
- Deflatten cleanup still uses view-level deflatten state maps; do not migrate
  them into function phase state unless that work is explicitly in scope.

`set_user_indirect_branches` uses Binary Ninja user-informed dataflow behavior and
can make resolved branches appear as `MLIL_JUMP_TO`/switch-like shapes. Preserve
the target-decode IL until branch condition translation has had a chance to read
it.

Phase cleanup is intentionally narrow. It only NOPs dead pure target-decode
assignments rooted at the owning phase's resolved sites. It must not collapse
control flow, NOP calls/stores, or remove deflatten state writes.

Cleanup is a one-shot attempt receipt:

- `branch.cleanup_done` is invalidated by branch target receipt changes.
- `call.cleanup_done` is invalidated by call target receipt changes.
- Branch target changes also invalidate the whole call phase.

MLIL rewrites are overlays. Function reanalysis can regenerate MLIL and erase
NOP/if/call-destination rewrites. The one-shot cleanup receipt exists to avoid
expensive repeated scans; if a later change must replay cleanup after every
reanalysis, update the phase-state design first.

Plugin hot reload is unreliable for workflow activity callbacks. After changing
workflow registration or callback code, prefer a full Binary Ninja GUI restart
for validation. Direct `bn py exec` callback invocation can prove the Python
logic, but it does not guarantee the GUI workflow has rebound the activity.

## Binary Ninja workflow/API reminders

Primary local docs to consult before changing this plugin:

- `D:\BN\docs\dev\workflows.html`
- `D:\BN\docs\dev\bnil-overview.html`
- `D:\BN\docs\dev\bnil-modifying.html`
- `D:\BN\docs\dev\uidf.html`
- `D:\BN\api-docs\binaryninja.workflow-module.html`

Binary Ninja workflows are DAGs of activities. Function workflows operate at
function granularity and can run independently/concurrently from module
workflow work. Activities are shared across workflows and their callbacks must
be re-entrant; do not store per-function pass state on the activity object or in
module globals.

Registered workflows are immutable. The normal customization pattern is
clone/modify/register, and `Workflow.insert(activity, activities)` inserts
before the named activity at the same level. After registration or ordering
changes, verify the actual GUI-bound workflow rather than trusting source order.

Workflow activity callbacks receive an `AnalysisContext`. Prefer the context's
current `function`, `llil`, `mlil`, and `hlil` when rewriting analysis state; the
context represents the in-progress pipeline, while `func.medium_level_il` and
friends may reflect regenerated or stale state depending on timing.

BNIL level choice matters:

- LLIL is closest to lifted machine semantics and is the right place to recover
  indirect branch targets early enough to rebuild CFG.
- MLIL has variables, dataflow, propagated constants, call-site information, and
  useful `PossibleValueSet` results; use it for indirect call resolving, condition translation,
  and target-decode cleanup.
- HLIL is presentation/structuring oriented. Avoid using HLIL as the source of
  truth for indirect branch/call resolving decisions.
- SSA forms are generated analysis products. Use SSA for def-use reasoning, but
  rewrite the corresponding non-SSA IL expression/instruction.

When modifying IL with `replace_expr`, call `finalize()` to rebuild IL basic
blocks and `generate_ssa_form()` before later passes depend on updated
dataflow. If a workflow activity creates a replacement MLIL function, install it
through `AnalysisContext.set_mlil_function(...)` rather than only mutating a
detached object.

UIDF primarily operates through MLIL/dataflow. Setting user-informed values or
indirect branches can trigger function reanalysis, simplify branches, and create
jump-table/switch-style output. In DispatchThis, that means target-decode IL
must remain available until the relevant branch/call phase is stable and its
translation/cleanup pass has consumed it.

Useful validation commands:

- `bn workflow active`
- `bn workflow show core.function.metaAnalysis --depth immediate`
- `bn api-docs show binaryninja.workflow.AnalysisContext --docs-dir D:\BN\api-docs`
- `bn docs show dev\workflows.html`

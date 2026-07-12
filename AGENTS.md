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

Naming should keep domain terms complete at module boundaries, but internal
workflow callbacks and state helpers should stay short. Prefer names like
`resolve_calls_mlil`, `branch_stable`, and `cleanup_decode`; avoid redundant
prefixes such as `workflow_` or long predicate phrases such as
`indirect_call_resolving_is_stable`.

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
- Global constant resolving runs after indirect call resolving and before
  branch condition translation. Branch translation runs only after branch,
  call, and global phases are stable so global type edits cannot erase an
  expensive MLIL overlay on the next reanalysis.
- Branch-target cleanup runs after branch condition translation.
- Call-target cleanup runs after indirect call resolving is stable and no new
  call type adjustment was submitted in that run.

Deflatten plans own their cleanup evidence. Each plan carries an
`obsolete_state_writes` set of exact current-MLIL instruction indices. If a
successor cannot be proved, do not emit the plan. If the successor is proved but
the state writes are not proved obsolete, keep the plan with an empty cleanup
set. Do not recover cleanup sites by scanning the function for matching tokens
or variables.

An unconditional plan carries every private dispatcher `exit_jumps` edge, and
all exits must replay the concrete state token to the same target. Conditional
target proof requires every CFG path in each arm to terminate at a dispatcher
entry; proving only that an exit is reachable is insufficient. Multiple valid
conditional candidates are ambiguous and must not be resolved by list or block
order. The
deflatten backend applies those edge rewrites, conditional rewrites, and exact
state-write NOPs in one atomic MLIL copy-transform; any invalid selected rewrite
discards the replacement. There is no separate deflatten cleanup activity or
state-token/state-variable session map.

Dispatcher routing supports variable/constant `MLIL_CMP_E`, `MLIL_CMP_NE`, and
signed or unsigned `LT`, `LE`, `GT`, and `GE`. Preserve operand order and token
width, then replay each concrete recovered token through the dispatcher CFG.
Do not infer symbolic token intervals or choose a target when replay is
ambiguous.

Conditional tails may contain only control flow and assignments on the proved
state-selection dependency chain. Every path must establish the same token;
the presence of one agreeing write somewhere in the scope is insufficient.
When each arm has a private GOTO directly into a dispatcher comparison row,
rewrite those exits so state writes still execute. Rewriting the original IF is
allowed only when the whole state channel is dispatcher-only and every skipped
write is proved private. Any external arm entry rejects the conditional plan:
even an arm-exit rewrite would also change the foreign path using that exit. A
profile that recognizes pointer-based state stores must prove the
store destination through one complete, unique definition chain that dominates
the STORE and ends at the state variable; a variable that once held `&state` is
not sufficient evidence.

Dispatcher replay may skip only NOP/GOTO routing blocks and direct variable
copies within the proved state-variable dependency chain. Reject selected
comparison rows with unrelated assignments, side effects, or state replacement
from a constant. Dispatcher-derived temporaries must have no observers outside
the dispatcher. Any rewritten entry/arm region must be private except for its
declared owner block; checking only the final exit block is insufficient. Here,
"direct variable" means a whole `MLIL_VAR`/`MLIL_VAR_SSA` read. Field, split,
and aliased reads are observer/alias evidence, never exact copies of the whole
state or pointer.

Every dispatcher comparison alias must have one unique, equal-width direct-copy
chain earlier in that same row, and selected rows must end at one shared state
input. Do not accept an alias only because an external definition traces to
state; it may be stale on another dispatcher entry. Treat field, split, and
aliased writes as possible state mutations, and treat `STORE_STRUCT` like other
pointer stores. An unresolved possible mutation rejects the transition.
`MLIL_ADDRESS_OF_FIELD` is an address escape wherever `MLIL_ADDRESS_OF` is.
Follow field, split, aliased, and `vars_read` uses when checking observers and
possible aliases. Do not impose a fixed definition-depth cutoff. Once a state
address is stored to memory or retained by an unknown operation (including via
`holder = &state; call(&holder)`), a later call/syscall/intrinsic/trap/breakpoint
or non-exact STORE is a possible state mutation even when it has no explicit
pointer parameter. `MLIL_UNIMPL` and `MLIL_UNIMPL_MEM` always reject a
transition because their state effect cannot be proved.

Variable identity is semantic evidence. Never compare, key, or de-duplicate
Binary Ninja variables/registers by `str(...)` or `repr(...)`; distinct storage
objects may share a display name. Normalize SSA wrappers explicitly, then use
the underlying object's equality/identity.

When an IF condition is a predicate variable, its comparison definition must be
a current non-SSA instruction earlier in the same row. Map an SSA definition
through `non_ssa_form` and verify its current instruction identity before use.
Use the comparison definition,
not the later IF, as the state-copy use point. Passing a possible state pointer
to a call, tail call, syscall, or intrinsic invalidates target proof. Exact
zero-offset pointer copies may be accepted only through a unique dominating
definition chain whose copies preserve the known pointer width; field values,
truncating copies, and other pointer arithmetic are unresolved possible
mutations.
Before applying a profile plan, verify each planned GOTO/IF still matches the
operation, expression identity, and address at its current MLIL instruction
index. Accept only non-negative exact `int` instruction indices (never booleans)
and require the current instruction to report that same index. Apply the same
exact-integer rule to every target basic-block start.

Only comparison rows whose entire routing prefix passes the purity proof may be
added to `dispatcher_starts` or excluded from observer checks. This applies to
non-dominant/auxiliary comparison rows as well as the selected dominant cluster.

`set_user_indirect_branches` uses Binary Ninja user-informed dataflow behavior and
can make resolved branches appear as `MLIL_JUMP_TO`/switch-like shapes. Preserve
the target-decode IL until branch condition translation has had a chance to read
it.

Phase cleanup is intentionally narrow. It only NOPs dead pure target-decode
assignments rooted at the owning phase's resolved sites. It must not collapse
control flow, NOP calls/stores, or remove deflatten state writes.

`BinaryView.session_data["dispatchthis_mlil_stable"]` is only a cross-function
gate for string decrypt recognition. Clear the current function's marker before
each deflatten attempt and publish it only after the atomic replacement is
installed successfully.

Cleanup receipts mean the current IL had no remaining phase-owned cleanup
changes, not merely that cleanup was attempted:

- `branch.cleanup_done` is invalidated by branch target receipt changes.
- `call.cleanup_done` is invalidated by call target receipt changes.
- Branch target changes also invalidate the whole call phase.

MLIL rewrites are overlays. Function reanalysis can regenerate MLIL and erase
NOP/if/call-destination rewrites. If branch/call phase cleanup NOPs anything,
keep its cleanup receipt open so the next workflow run can replay or confirm the
overlay after reanalysis. Deflatten edge and exact state-write rewrites are
recomputed and applied together on each eligible workflow run.

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

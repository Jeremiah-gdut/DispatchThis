# The pipeline

DispatchThis registers a **clone of `core.function.metaAnalysis`** and inserts its
own activities into it. Everything is IL expression rewriting - no bytes are patched.

## Registration and ordering

From `__init__.py` / `workflow.py`, these activities are inserted:

| Activity ID | Stage | Inserted before |
| --- | --- | --- |
| `analysis.plugins.dispatchThis.indirectJumpsCalls` | LLIL toggle | `core.function.generateMediumLevelIL` |
| `extension.DispatchThis.IndirectPatcher` | LLIL | `core.function.generateMediumLevelIL` |
| `extension.DispatchThis.IndirectCallPatcher` | MLIL | `core.function.generateHighLevelIL` |
| `extension.DispatchThis.BranchConditionTranslator` | MLIL | `core.function.generateHighLevelIL` |
| `extension.DispatchThis.GlobalConstantResolver` | MLIL | `core.function.generateHighLevelIL` |
| `analysis.plugins.dispatchThis.deflatten` | MLIL | `core.function.generateHighLevelIL` |
| `extension.DispatchThis.Cleanup` | MLIL | `core.function.generateHighLevelIL` |

The indirect-jump resolver runs **before MLIL is generated**, because the deflattener needs
the flattened CFG to exist (the indirect jumps resolved to real edges) before MLIL analysis.
The other five run before HLIL generation, in the order call-resolve → branch-condition
translation → global-constant resolving → deflatten → cleanup. The MLIL activities gate themselves on function phase
state, so they do not submit reanalysis-triggering mutations until indirect branch
resolving is stable. Workflow callbacks own reanalysis-triggering Binary Ninja edits:
`set_user_indirect_branches`, `set_call_type_adjustment`, global data-var typing, and
analysis-completion callback scheduling.

## The activities

### 1. Indirect jump resolver (LLIL) - `passes/low/gadget_llil.py`

`resolve_llil_jump_plan` parses each decode-gadget `jump(reg)` (and tail-call form),
decodes its target(s) from the relocated jump table, and returns a read-only plan.
`apply_llil_jump_rewrites` rewrites single-target jumps in the current LLIL. The workflow
callback owns the reanalysis-triggering `set_user_indirect_branches` mutation and records
a per-function receipt for each source. Once branch resolving is stable, the workflow also
schedules the `Unresolved Indirect Control Flow` tag cleanup with
`BinaryView.add_analysis_completion_event`.

Because the function grows, the workflow re-runs and the next layer resolves, **iterating
to a fixpoint** with no manual loop and no byte patching. Targets are resolved read-only
first, then all current-IL rewrites are applied and SSA is rebuilt once. Branch resolving
is stable only when every unresolved indirect-branch source is covered by user branch
metadata and the current run did not submit a new branch mutation.

### 2. Indirect call resolver (MLIL) - `passes/medium/indirect_calls.py`

`plan_indirect_calls` folds each import call's decode (`target = (encoded + key) mod
2^48`) without mutating function state. `apply_indirect_call_rewrites` rewrites the call's
**destination expression** into a `const_pointer` and folds the spilled decode definition
(`var = encoded + key` → `var = const`) so the dead decode collapses cleanly.

After the destination is a bare constant, the call carries only calling-convention guesses
and no prototype, so HLIL would render arguments as `/* nop */`. The workflow fixes this by
**pinning the callee prototype** at the call site via `set_call_type_adjustment`.

> [!IMPORTANT]
> `set_call_type_adjustment` is a *function-level* edit that schedules a fresh reanalysis
> (unlike `replace_expr`, which the current pass simply consumes). Applying it every run
> would loop analysis forever, so workflow records per-function call adjustment receipts
> in `Function.session_data["dispatchthis_workflow_state"]`.

### 3. Branch condition translator (MLIL) - `passes/medium/branch_conditions.py`

`set_user_indirect_branches` uses Binary Ninja's user-informed dataflow behavior, so
two-target indirect branches can appear as resolved `switch`/`MLIL_JUMP_TO` shapes.
After indirect branch resolving is stable, the translator rewrites those two-target
switches back into `MLIL_IF` expressions. This is a repeatable presentation rewrite and
does not own mutation receipts.

### 4. Global constant resolver (MLIL) - `passes/medium/global_constants.py`

`plan_global_constant_slots` recognizes narrow writable-section global pointer slots that
are only used as read-only constant bases. The workflow callback applies the
BinaryView-level `define_user_data_var` mutation with a `uint8_t const* const` type and
records a view-level receipt so several functions do not retype the same slot.

The first scope is intentionally narrow: a qword slot in `.data`, a nonzero constant
offset chain, a valid resolved address, and no store to the slot in the known direct-ref
functions.

### 5. Deflattener (MLIL, opt-in) - `passes/medium/deflatten.py`

Gated behind the `Enable Deflattening` setting, and only runs once the LLIL stage has
drained every indirect jump (otherwise the CFG - and the recovered state machine - would be
incomplete).

- `compute_redirections` identifies the dominant dispatcher comparison cluster from
  state-token compares and maps each token to its target original block.
- `apply_redirections_il` rewrites each `OBB → dispatcher` terminator into a direct
  `goto` to the real successor. Conditional transitions are reconstructed when each
  branch arm selects exactly one known state token - see
  [`conditional-deflattening.md`](conditional-deflattening.md).
- The resolved dispatcher state values and the state variable's alias set are recorded to
  `session_data` so the cleanup can NOP the state writes precisely (by value and by var).

### 6. Deflatten cleanup / NOP pass (MLIL, opt-in) - `passes/medium/nop_pass.py`

Gated behind `Deflatten`; it only acts once deflatten has rewritten the OBB exits.
`nop_deflatten_state_writes` NOPs dispatcher state writes by the state token values and
state variables recorded by the deflatten workflow. Branch-target and call-target decode
cleanup are separate phase cleanup attempts owned by the workflow callbacks for those
phases.

## Why the MLIL passes reapply every run

The indirect call, branch condition, deflatten, and final state-write cleanup MLIL rewrites are *overlays*
derived from the (unchanged) LLIL. Each reanalysis regenerates MLIL from LLIL and reverts
them, so these passes **re-run every pass** to keep their rewrites in place rather than
latching off after the first apply. Phase cleanup for branch/call target decodes is
receipt-gated and only reruns after its phase receipts change.

## `session_data` keys

| Key | Meaning |
| --- | --- |
| `dispatchthis_llil_stable` | `{start: bool}` - LLIL indirect jumps fully resolved |
| `dispatchthis_mlil_stable` | `{start: bool}` - deflatten has rewritten exits |
| `dispatchthis_state_consts` | `{start: set(state_value)}` - for state-write NOP |
| `dispatchthis_state_vars` | `{start: set(var)}` - state var + aliases |
| `dispatchthis_tag_cleanup_pending` | `set(start)` - view-level analysis-completion callbacks pending |
| `dispatchthis_global_constant_slots` | `{slot_addr: type_string}` - view-level global constant type receipts |

Function-scoped phase state lives in `Function.session_data["dispatchthis_workflow_state"]`:

| Field | Meaning |
| --- | --- |
| `branch.stable` | indirect branch resolving has reached its current fixpoint |
| `branch.receipts` | `{source_addr: (target_addr, ...)}` submitted as user branch metadata |
| `branch.cleanup_done` | branch-target decode cleanup has run for the current branch receipts |
| `call.stable` | indirect call resolving has reached its current fixpoint |
| `call.receipts` | `{call_addr: target_addr}` submitted as call type adjustments |
| `call.targets` | `{call_addr: target_addr}` resolved as call destinations, even when no type adjustment was submitted |
| `call.cleanup_done` | call-target decode cleanup has run for the current call receipts |

## Analysis limits

The plugin raises several Binary Ninja analysis limits at import (max function size,
expression-value compute depth, max analysis time, max update count) because flattened
functions are large and need many reanalysis passes to reach a fixpoint.

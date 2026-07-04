# The pipeline

DispatchThis registers a **clone of `core.function.metaAnalysis`** and inserts its
own activities into it. Everything is IL expression rewriting - no bytes are patched.

## Registration and ordering

From `__init__.py` / `workflow.py`, five activities are inserted:

| Activity ID | Stage | Inserted before |
| --- | --- | --- |
| `extension.DispatchThis.IndirectPatcher` | LLIL | `core.function.generateMediumLevelIL` |
| `extension.DispatchThis.IndirectCallPatcher` | MLIL | `core.function.generateHighLevelIL` |
| `extension.DispatchThis.BranchConditionTranslator` | MLIL | `core.function.generateHighLevelIL` |
| `extension.DispatchThis.Deflattener` | MLIL | `core.function.generateHighLevelIL` |
| `extension.DispatchThis.Cleanup` | MLIL | `core.function.generateHighLevelIL` |

The indirect-jump resolver runs **before MLIL is generated**, because the deflattener needs
the flattened CFG to exist (the indirect jumps resolved to real edges) before MLIL analysis.
The other four run before HLIL generation, in the order call-resolve → branch-condition
translation → deflatten → cleanup. The MLIL activities gate themselves on function phase
state, so they do not submit reanalysis-triggering mutations until indirect branch
resolving is stable.

## The activities

### 1. Indirect jump resolver (LLIL) - `passes/low/gadget_llil.py`

`resolve_llil_jump_plan` parses each decode-gadget `jump(reg)` (and tail-call form),
decodes its target(s) from the relocated jump table, and returns a read-only plan.
`apply_llil_jump_rewrites` rewrites single-target jumps in the current LLIL. The workflow
callback owns the reanalysis-triggering `set_user_indirect_branches` mutation and records
a per-function receipt for each source.

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

### 4. Deflattener (MLIL, opt-in) - `passes/medium/deflatten.py`

Gated behind the `Enable Deflattening` setting, and only runs once the LLIL stage has
drained every indirect jump (otherwise the CFG - and the recovered state machine - would be
incomplete).

- `StateMachine(bv, func).analyze()` (`utils/state_machine.py`) recovers the state
  variable, the backbone `{state_value -> comparator block}`, and each OBB's real
  successor(s).
- `compute_redirections` + `apply_redirections_il` rewrite each `OBB → dispatcher`
  `MLIL_JUMP_TO` into a direct `goto` to the real successor. Conditional (cmov-selected)
  transitions are reconstructed into `if`/branch control flow using Z3 - see
  [`conditional-deflattening.md`](conditional-deflattening.md).
- The resolved dispatcher state values and the state variable's alias set are recorded to
  `session_data` so the cleanup can NOP the state writes precisely (by value and by var).

### 5. Cleanup / NOP pass (MLIL, opt-in) - `passes/medium/nop_pass.py`

Gated behind `Enable Cleanup` **and** `Enable Deflattening`; it only acts once deflatten has
rewritten the OBB exits this pass. `clean_resolved_gadget_jumps` then, to a fixpoint:

- converts every single-target `MLIL_JUMP_TO` into a `goto`;
- collapses each always-true opaque predicate (its condition reads a gadget-tainted
  variable, and its branches reconverge) into a `goto` the common join;
- NOPs gadget-tainted pure assignments, the dead decode residue, and the state writes.

Gadgets are identified by **signature** (the 64-bit decode keys and the repeatedly-loaded
table slots), not by slicing the already-folded jump. The safety floor: only pure
assignments / phis are ever NOP'd - never a call, store, or control-flow instruction.

## Why the MLIL passes reapply every run

The indirect call, branch condition, deflatten, and cleanup MLIL rewrites are *overlays*
derived from the (unchanged) LLIL. Each reanalysis regenerates MLIL from LLIL and reverts
them, so these passes **re-run every pass** to keep their rewrites in place rather than
latching off after the first apply. Deflatten runs before cleanup so that cleanup sees the gotos and leaves the
`OBB → dispatcher` exits alone.

## `session_data` keys

| Key | Meaning |
| --- | --- |
| `dispatchthis_llil_stable` | `{start: bool}` - LLIL indirect jumps fully resolved |
| `dispatchthis_gadget_map` | `{start: {jump_addr: target}}` - resolved jump targets |
| `dispatchthis_mlil_stable` | `{start: bool}` - deflatten has rewritten exits |
| `dispatchthis_state_consts` | `{start: set(state_value)}` - for state-write NOP |
| `dispatchthis_state_vars` | `{start: set(var)}` - state var + aliases |
| `dispatchthis_tag_cleanup_pending` | `set(start)` - view-level analysis-completion callbacks pending |

Function-scoped phase state lives in `Function.session_data["dispatchthis_workflow_state"]`:

| Field | Meaning |
| --- | --- |
| `branch.stable` | indirect branch resolving has reached its current fixpoint |
| `branch.receipts` | `{source_addr: (target_addr, ...)}` submitted as user branch metadata |
| `call.stable` | indirect call resolving has reached its current fixpoint |
| `call.receipts` | `{call_addr: target_addr}` submitted as call type adjustments |

## Analysis limits

The plugin raises several Binary Ninja analysis limits at import (max function size,
expression-value compute depth, max analysis time, max update count) because flattened
functions are large and need many reanalysis passes to reach a fixpoint.

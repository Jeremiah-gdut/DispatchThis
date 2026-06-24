# The pipeline

DispatchThis registers a **clone of `core.function.metaAnalysis`** and inserts its
own activities into it. Everything is IL expression rewriting - no bytes are patched.

## Registration and ordering

From `__init__.py` / `workflow.py`, four activities are inserted:

| Activity ID | Stage | Inserted before |
| --- | --- | --- |
| `extension.DispatchThis.IndirectPatcher` | LLIL | `core.function.generateMediumLevelIL` |
| `extension.DispatchThis.IndirectCallPatcher` | MLIL | `core.function.generateHighLevelIL` |
| `extension.DispatchThis.Deflattener` | MLIL | `core.function.generateHighLevelIL` |
| `extension.DispatchThis.Cleanup` | MLIL | `core.function.generateHighLevelIL` |

The indirect-jump resolver runs **before MLIL is generated**, because the deflattener needs
the flattened CFG to exist (the indirect jumps resolved to real edges) before MLIL analysis.
The other three run before HLIL generation, in the order call-resolve → deflatten → cleanup.

## The activities

### 1. Indirect jump resolver (LLIL) - `passes/low/gadget_llil.py`

`resolve_and_rewrite_llil_jumps`. Parses each decode-gadget `jump(reg)` (and tail-call
form), decodes its target from the relocated jump table, and rewrites the jump destination
into `jump(const)`. A constant jump target is a *direct* branch, so Binary Ninja then
disassembles the target, defines it as code, and reconnects the CFG - which exposes the
next layer of gadgets.

Because the function grows, the workflow re-runs and the next layer resolves, **iterating
to a fixpoint** with no manual loop and no byte patching. Targets are resolved read-only
first, then all rewrites are applied and SSA is rebuilt once. When no jumps remain, the
function is marked stable (`dispatchthis_llil_stable[start] = True`).

### 2. Indirect call resolver (MLIL) - `passes/medium/indirect_calls.py`

`patch_indirect_calls`. Folds each import call's decode (`target = (encoded + key) mod
2^48`) and rewrites the call's **destination expression** into a `const_pointer`. It also
folds the spilled decode definition (`var = encoded + key` → `var = const`) so the dead
decode collapses cleanly.

After the destination is a bare constant, the call carries only calling-convention guesses
and no prototype, so HLIL would render arguments as `/* nop */`. The pass fixes this by
**pinning the callee prototype** at the call site via `set_call_type_adjustment`.

> [!IMPORTANT]
> `set_call_type_adjustment` is a *function-level* edit that schedules a fresh reanalysis
> (unlike `replace_expr`, which the current pass simply consumes). Applying it every run
> would loop analysis forever, so it is applied **at most once per call site per session**,
> tracked in `dispatchthis_call_types_set`.

### 3. Deflattener (MLIL, opt-in) - `passes/medium/deflatten.py`

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

### 4. Cleanup / NOP pass (MLIL, opt-in) - `passes/medium/nop_pass.py`

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

The deflatten and cleanup MLIL rewrites are *overlays* derived from the (unchanged) LLIL.
Each reanalysis regenerates MLIL from LLIL and reverts them, so both passes **re-run every
pass** to keep their rewrites in place rather than latching off after the first apply.
Deflatten runs before cleanup so that cleanup sees the gotos and leaves the
`OBB → dispatcher` exits alone.

## `session_data` keys

| Key | Meaning |
| --- | --- |
| `dispatchthis_llil_stable` | `{start: bool}` - LLIL indirect jumps fully resolved |
| `dispatchthis_gadget_map` | `{start: {jump_addr: target}}` - resolved jump targets |
| `dispatchthis_mlil_stable` | `{start: bool}` - deflatten has rewritten exits |
| `dispatchthis_state_consts` | `{start: set(state_value)}` - for state-write NOP |
| `dispatchthis_state_vars` | `{start: set(var)}` - state var + aliases |
| `dispatchthis_call_types_set` | `{start: set(call_addr)}` - once-guard for type adjust |

## Analysis limits

The plugin raises several Binary Ninja analysis limits at import (max function size,
expression-value compute depth, max analysis time, max update count) because flattened
functions are large and need many reanalysis passes to reach a fixpoint.

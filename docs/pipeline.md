# The pipeline

DispatchThis registers a **clone of `core.function.metaAnalysis`** and inserts its
own activities into it. Everything is IL expression rewriting - no bytes are patched.

## Registration and ordering

From `__init__.py` / `workflow.py`, eight activities are inserted. The
`analysis.plugins.dispatchThis.indirectJumpsCalls` activity is a no-op setting activity;
the others are recovery workflow phases:

| Activity ID | Stage | Inserted before |
| --- | --- | --- |
| `analysis.plugins.dispatchThis.indirectJumpsCalls` | LLIL toggle | `core.function.generateMediumLevelIL` |
| `extension.DispatchThis.IndirectPatcher` | LLIL | `core.function.generateMediumLevelIL` |
| `extension.DispatchThis.IndirectCallPatcher` | MLIL | `core.function.generateHighLevelIL` |
| `extension.DispatchThis.BranchConditionTranslator` | MLIL | `core.function.generateHighLevelIL` |
| `extension.DispatchThis.GlobalConstantResolver` | MLIL | `core.function.generateHighLevelIL` |
| `extension.DispatchThis.CorrelatedStoreRecovery` | MLIL | `core.function.generateHighLevelIL` |
| `analysis.plugins.dispatchThis.stringDecrypt` | MLIL | `core.function.generateHighLevelIL` |
| `analysis.plugins.dispatchThis.deflatten` | MLIL | `core.function.generateHighLevelIL` |

The indirect branch resolver runs **before MLIL is generated**, because the deflattener needs
the flattened CFG to exist (the indirect jumps resolved to real edges) before MLIL analysis.
The other six run before HLIL generation, in the order call-resolve → branch-condition
translation → global-constant resolving → correlated-store recovery → string
decrypt → deflatten. The MLIL activities gate themselves on function phase
state, so they do not submit reanalysis-triggering mutations until indirect branch
resolving is stable. Workflow callbacks own reanalysis-triggering Binary Ninja edits:
`set_user_indirect_branches`, `set_call_type_adjustment`, global data-var typing, and
analysis-completion callback scheduling.
The coordination rules are captured in
[`adr/0003-function-phase-state-for-workflow.md`](adr/0003-function-phase-state-for-workflow.md).
New binary recognizers should be added as bundled resolver profiles; see
[`resolver-profiles.md`](resolver-profiles.md).

## The activities

### 1. Indirect branch resolver (LLIL) - `passes/low/gadget_llil.py`

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
does not own mutation receipts. After translation, workflow runs branch-target phase
cleanup rooted at the resolved branch sites.

### 4. Global constant resolver (MLIL) - `passes/medium/global_constants.py`

`plan_global_constant_slots` recognizes narrow writable-section global pointer slots that
are only used as read-only constant bases. The workflow callback applies the
BinaryView-level `define_user_data_var` mutation with a `uint8_t const* const` type,
treats the current data-variable type as view-level truth, and records a per-function
receipt. The current function's global phase becomes stable only after its receipts
still match the BinaryView types.

The first scope is intentionally narrow: a qword slot in `.data`, a nonzero constant
offset chain, a valid resolved address, and no store to the slot in the known direct-ref
functions.

### 5. Correlated store recovery (MLIL) - `passes/medium/correlated_stores.py`

After global constants stabilize, the active profile may identify a join-block store whose
destination and source came from correlated sibling PHIs. `apply_correlated_stores_mlil`
atomically inserts each concrete store in its owning predecessor arm and NOPs the merged
store. Unsupported or incomplete plans leave the current MLIL unchanged.

### 6. String decrypt (MLIL, opt-in) - `passes/medium/string_decrypt.py`

Gated behind the `String Decrypt` setting. The workflow callback returns without work
until indirect branch, indirect call, and global constant phases are stable. It does not
require the current function to be deflattened first.

`annotate_decrypted_string_calls` scans only direct MLIL calls in the current workflow
function. A candidate callee must already be marked deflattened in
`dispatchthis_mlil_stable` and match the sample-family decrypt-function shape: two
arguments, key-prefix reads, encrypted payload reads, a fixed key modulus, fixed output
length, byte writes to the destination buffer, and a one-shot done flag write. Matching
calls get function-level comments in the form
`[decrypt] <escaped-string>, src=0x... dst=0x...`; existing manual comment lines are
preserved.

### 7. Deflattener (MLIL, opt-in) - `passes/medium/deflatten.py`

Gated behind the `Enable Deflattening` setting, and only runs once function phase state
reports that the LLIL indirect branch resolver has drained every indirect jump (otherwise
the CFG - and the recovered state machine - would be incomplete).

- The active resolver profile's `plan_deflatten_redirections` identifies the
  binary-specific dispatcher/state-write shape and maps state tokens to target
  original blocks. Dispatcher rows may use equality, inequality, or signed/unsigned
  `LT`, `LE`, `GT`, and `GE` comparisons. The planner preserves operand order and token
  width, then replays each concrete recovered token through the dispatcher CFG; it does
  not solve symbolic intervals. Each comparison alias must be established by a unique
  whole-variable, equal-width direct-copy chain earlier in its own row, ending at the
  state input shared by the dispatcher rows. Field/split/aliased reads are possible
  observers, not exact copies. Predicate-variable conditions must resolve through
  exact SSA-to-non-SSA mapping to a current comparison earlier in that row, and the
  copy chain must precede the comparison itself. Auxiliary comparison blocks join
  the dispatcher boundary only after their complete prefix passes the routing-purity
  proof. The
  default profile delegates to `compute_redirections`.
- `rewrite_redirections_mlil` uses the MLIL copy-transform backend to build an atomic
  replacement: every private dispatcher exit is redirected to the one target proved for
  it, conditional transitions explicitly choose either private arm-exit rewrites or a
  fully proved condition shortcut, and only exact instruction indices in each plan's
  `obsolete_state_writes` set become NOPs - see
  [`conditional-deflattening.md`](conditional-deflattening.md). Any rejected redirection
  discards the entire replacement.
- Target and cleanup proof are independent when the selected edge rewrite preserves state
  execution. An uncertain target produces no plan; a proved target with uncertain cleanup
  then keeps an empty `obsolete_state_writes` set. A condition shortcut that would bypass
  those writes instead requires complete private cleanup/state-channel proof or is rejected.
- Partial/split/aliased state writes, unresolved struct or pointer stores, and whole-variable
  or field-address escapes are fail-closed rather than ignored as unrelated IL. A call,
  syscall, or intrinsic receiving a possible state pointer invalidates target proof, not
  merely cleanup proof. Once that address has escaped into memory, later unknown
  memory effects or non-exact stores invalidate the token even without an explicit
  pointer argument. Escape includes an unknown operation retaining `&holder` when
  the holder contains `&state`. Unimplemented IL always rejects the transition.
- The workflow installs the replacement through `AnalysisContext.set_mlil_function`, then
  publishes `dispatchthis_mlil_stable` for cross-function string-decrypt recognition. It
  publishes no deflatten token or variable cleanup maps.

The cleanup ownership and atomicity decision is recorded in
[`adr/0010-plan-owned-atomic-deflatten-cleanup.md`](adr/0010-plan-owned-atomic-deflatten-cleanup.md).

## Why the MLIL passes reapply every run

The indirect call, branch condition, correlated-store, and atomic deflatten MLIL rewrites are *overlays*
derived from the (unchanged) LLIL. Each reanalysis regenerates MLIL from LLIL and reverts
them, so these passes **re-run every pass** to keep their rewrites in place rather than
latching off after the first apply. Phase cleanup for branch/call target decodes is
receipt-gated, but the receipt is marked done only after the current IL has no cleanup
changes left; if cleanup NOPs anything, the next workflow run can replay or confirm the
overlay after Binary Ninja reanalysis.

## `session_data` keys

| Key | Meaning |
| --- | --- |
| `dispatchthis_mlil_stable` | `{start: bool}` - the atomic deflatten replacement was installed; used only as a cross-function string-decrypt gate |
| `dispatchthis_tag_cleanup_pending` | `set(start)` - view-level analysis-completion callbacks pending |

Function-scoped phase state lives in `Function.session_data["dispatchthis_workflow_state"]`;
see [`adr/0003-function-phase-state-for-workflow.md`](adr/0003-function-phase-state-for-workflow.md)
for the workflow coordination rules:

| Field | Meaning |
| --- | --- |
| `branch.stable` | indirect branch resolving has reached its current fixpoint |
| `branch.receipts` | `{source_addr: (target_addr, ...)}` submitted as user branch metadata |
| `branch.cleanup_done` | branch-target decode cleanup found no remaining changes for the current branch receipts |
| `call.stable` | indirect call resolving has reached its current fixpoint |
| `call.receipts` | `{call_addr: target_addr}` submitted as call type adjustments |
| `call.targets` | `{call_addr: target_addr}` resolved as call destinations, even when no type adjustment was submitted |
| `call.cleanup_done` | call-target decode cleanup found no remaining changes for the current call receipts |
| `global.stable` | global constant resolving has reached its current fixpoint for this function |
| `global.receipts` | `{slot_addr: type_string}` verified as global constant data-var types for this function |

## Analysis environment

On Binary Ninja 5.3+, the earliest eligible resolver callback establishes the required
analysis environment on the current Function, not when DispatchThis is imported. It uses
`SettingsResourceScope` to override inherited values only when needed, then reads all of
them back before profile recognition or recovery work. A failed write or verification skips
that workflow run; Function overrides are intentionally left in place after DispatchThis is
disabled.

| Setting | Required value |
| --- | --- |
| `analysis.limits.maxFunctionSize` | `0` (unlimited) |
| `analysis.limits.expressionValueComputeMaxDepth` | `99999` |
| `analysis.limits.maxFunctionAnalysisTime` | `600000` ms |
| `analysis.limits.maxFunctionUpdateCount` | `1024` |
| `analysis.outlining.builtins` | `false` |

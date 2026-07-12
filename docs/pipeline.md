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
The other six run before HLIL generation, in the order call-resolve → global-constant
resolving → branch-condition translation → correlated-store recovery → string
decrypt → deflatten. The MLIL activities gate themselves on function phase
state, so they do not submit reanalysis-triggering mutations until indirect branch
resolving is stable. Workflow callbacks own reanalysis-triggering Binary Ninja edits:
`set_user_indirect_branches`, `set_call_type_adjustment`, global data-var typing, and
analysis-completion callback scheduling.
The coordination rules are captured in
[`adr/0003-function-phase-state-for-workflow.md`](adr/0003-function-phase-state-for-workflow.md).
Complete-evidence and current-witness rules are captured in
[`adr/0011-complete-evidence-and-current-il-witnesses.md`](adr/0011-complete-evidence-and-current-il-witnesses.md).
Plan-owned call load cleanup is captured in
[`adr/0012-call-target-slice-owned-load-cleanup.md`](adr/0012-call-target-slice-owned-load-cleanup.md).
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

Each plan retains the current LLIL jump witness. Before a rewrite or metadata
submission, the pass groups facts by source and requires one complete,
non-conflicting semantic result whose operation, address, instruction/expression
identity, destination expression, and IL owner still match current LLIL. A receipt
alone never suppresses decoding. Only a receipt whose complete target tuple exactly
matches Binary Ninja's current non-auto user branch metadata is outside the next
decode frontier; missing, automatic, subset, superset, or changed metadata forces
fresh recognition. This avoids repeatedly parsing the `LLIL_JUMP_TO` shape that
user-informed dataflow creates for already resolved branches without hiding new work.
An unmatched gadget shape is a debug event because an expanding CFG commonly exposes
an intermediate shape before the next reanalysis; malformed, conflicting, or stale
branch facts remain warnings.

Because the function grows, the workflow re-runs and the next layer resolves, **iterating
to a fixpoint** with no manual loop and no byte patching. Targets are resolved read-only
first, then single-target current-IL rewrites are applied and SSA is rebuilt once.
Multi-target plans do not enter the rewrite backend because their CFG is represented by
user branch metadata rather than a constant jump destination. Branch resolving is stable
only when every unresolved indirect-branch source is covered by user branch metadata and
the current run did not submit a new branch mutation.

### 2. Indirect call resolver (MLIL) - `passes/medium/indirect_calls.py`

`plan_indirect_calls` folds each import call's decode (`target = (encoded + key) mod
2^48`) without mutating function state. `apply_indirect_call_rewrites` pre-validates the
complete plan batch, creates every replacement, then uses `replace_expr` to change only
each current call **destination expression** to a `const_pointer`. It finalizes MLIL and
regenerates SSA once for the batch. The workflow does not copy the whole function or call
`AnalysisContext.set_mlil_function` for these expression-only overlays; doing so would
force Binary Ninja to rebuild the complete LLIL-to-MLIL mappings. The pass deliberately
does not rewrite a profile-provided `decode_def`; dead decode instructions are owned only
by the recomputed SSA target slice and phase cleanup.
Call and descriptive decode witnesses are rebound to the exact current non-SSA MLIL
before call-destination or call-type mutation; stale profile facts fail closed. The decode
witness itself is never rewritten.

Each call plan also owns the exact current SSA reaching-definition slice feeding only
`call.dest`. PHIs expand to all inputs; only whole-variable SSA definitions that map to
exact current non-SSA assignments become cleanup roots. Field, split, and aliased chains
fail closed. Load assignments receive a separate `cleanup_load_roots` witness, because
generic cleanup must continue treating loads as observable. Both root sets are recomputed
from the current call at the mutation boundary, so stale/profile indices cannot authorize
cleanup. After the destination rewrite, current SSA liveness may NOP a witnessed load
only when its result has no use outside the now-obsolete target slice. This proof uses
call-site dataflow, not BinaryView xrefs; callback arguments and any other real consumer
therefore keep the assignment live. Calls, stores, intrinsics, unimplemented IL, and
other behavior instructions are never admitted merely because they precede a call. A
stored call receipt proves the callee, not cleanup ownership, so the workflow never
reconstructs roots by scanning assignments before a receipt address.

After the destination is a bare constant, the call carries only calling-convention guesses
and no prototype, so HLIL could render arguments as `/* nop */`. The workflow builds a
call-site type whose parameters come from the current MLIL argument expressions; the
callee contributes only its return type, calling convention, and related ABI metadata.
It installs that type via `set_call_type_adjustment`.

> [!IMPORTANT]
> `set_call_type_adjustment` is a *function-level* edit that schedules a fresh reanalysis
> (unlike `replace_expr`, which the current pass simply consumes). Applying it every run
> would loop analysis forever, so workflow records per-function call adjustment receipts
> in `Function.session_data["dispatchthis_workflow_state"]`.

The receipt is not the source of truth: for each safe concrete override, every run compares
the desired prototype with `get_call_type_adjustment`, submits only a real difference, and
reads it back before marking the call adjusted. Current call-site arguments therefore
survive even when the callee is itself obfuscated and BN has inferred an empty or incomplete
parameter list. Current fallthrough also overrides a premature noreturn inference. If the
callee has no usable function type or any call-site argument has no usable expression type,
workflow applies no override instead of inventing one. In particular it does not call
`set_call_type_adjustment(addr, None)` or try to compare `None` with BN's effective
automatically inferred type: clearing a user override can still expose that automatic type
and would otherwise keep the workflow re-entering.

### 3. Branch condition translator (MLIL) - `passes/medium/branch_conditions.py`

`set_user_indirect_branches` uses Binary Ninja's user-informed dataflow behavior, so
two-target indirect branches can appear as resolved `switch`/`MLIL_JUMP_TO` shapes.
After indirect branch and global constant resolving are stable, the translator rewrites
those two-target switches back into `MLIL_IF` expressions. Keeping this expensive CFG copy
after global data-var edits and their required reanalysis avoids installing the same
overlay twice. When the switch is the tail of an already existing
`IF -> two private constant-selector arms -> one decode join` diamond, it redirects that
source `IF` in place instead of creating a second condition at the join. Both decoded
targets must be unique and every independently valid selector witness must agree. The arms
and join must be private, side-effect-free target-decode code whose written variables are
local to the diamond and belong to the jump-destination dependency chain. If that ownership
proof fails, the translator keeps the existing join-site behavior rather than bypassing
unknown code. A selector assigned in the arms must remain unchanged through the join prefix;
when a fallback moves the source predicate to the join, that predicate must also remain
unchanged through both arms and the join prefix. Rewriting the original source IF in place
does not move its predicate and therefore does not impose that latter restriction. This
repeatable presentation rewrite owns no mutation receipts. Its exact
contiguous source assignment prefix is submitted as a branch cleanup root; SSA liveness
keeps state/comparison copies and NOPs only dead target-decode results.

The ownership planner builds two lazy indexes for only the current translator
invocation: one shared alias graph from all store/unknown-memory-effect roots,
and one map from variables to read/address-taken basic blocks. This preserves the
same fail-closed escape and scope-locality proofs without rescanning the whole
function for every candidate diamond. Neither index survives an MLIL mutation or
reanalysis. If at least one control-flow plan is accepted, all selected top-level
`IF`/`JUMP_TO` replacements are installed through one MLIL copy-transform because
they share copied labels and change CFG edges; an empty plan does not copy or
install an MLIL function.

### 4. Global constant resolver (MLIL) - `passes/medium/global_constants.py`

The active profile's `plan_global_constant_slots` returns proved slot/type facts.
The workflow parses each fact's const-qualified type, applies the BinaryView-level
`define_user_data_var` mutation, reads the current data-variable type back as
view-level truth, and records a per-function receipt. Conflicting facts for one
slot are rejected. The current function's global phase becomes stable only after
all receipts still match the BinaryView types.

The default profile's scope is intentionally narrow: a qword slot in `.data`, a
nonzero constant offset chain, a valid resolved address, and no store to the slot
in the known direct-ref functions. Other profiles may prove different shapes and
types without moving mutation ownership out of workflow.

### 5. Correlated store recovery (MLIL) - `passes/medium/correlated_stores.py`

After global constants stabilize, the active profile may identify a join-block store whose
destination and source came from correlated sibling PHIs. `apply_correlated_stores_mlil`
atomically inserts each concrete store in its owning predecessor arm and NOPs the merged
store. Unsupported or incomplete plans leave the current MLIL unchanged.

### 6. String decrypt (MLIL, opt-in) - `passes/medium/string_decrypt.py`

Gated behind the `String Decrypt` setting. The workflow callback returns without work
until indirect branch, indirect call, and global constant phases are stable. It does not
require the current function to be deflattened first.

The active profile's `plan_string_decrypt_calls` inspects the current MLIL and
returns plaintext facts without writing comments. Profiles may use
`dispatchthis_mlil_stable` to require a candidate callee to have a successfully
installed deflatten replacement. The shared backend
`apply_decrypted_string_comments` turns accepted facts into function-level comments
in the form
`[decrypt] <escaped-string>, src=0x... dst=0x...`; existing manual comment lines are
preserved.

The default profile recognizes only direct calls to its sample-family decrypt
shape: two arguments, key-prefix and encrypted-payload reads, one complete
key-modulus/output-length pair, byte writes to the destination, and a one-shot
done-flag write. Other profiles own their own complete recognition proof.

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
  proof. Branch-condition translation removes a proved private decode diamond before this
  analysis, so deflatten does not carry a second recognizer for a synthetic translated-tail
  shape. A separate OBB state variable may be mapped to
  the comparison variable only through one equal-width, whole-variable latch that is the
  unique dispatcher ingress and is shared by at least two independent target-head regions.
  Backward boundary expansion outside that explicit latch accepts only `NOP* + GOTO`
  blocks. The default profile delegates to `compute_redirections`.
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
| `profile_id` | resolver profile provenance for function-scoped evidence; state containing recovery evidence cannot be rebound to another profile, while empty state may be rebound |
| `branch.stable` | indirect branch resolving has reached its current fixpoint |
| `branch.receipts` | `{source_addr: (target_addr, ...)}` verified against current user branch metadata |
| `branch.cleanup_done` | branch-target decode cleanup found no remaining changes for the current branch receipts |
| `call.stable` | indirect call resolving has reached its current fixpoint |
| `call.receipts` | `{call_addr: target_addr}` whose call-type decision completed: a concrete override was read back, or current call-site evidence required no override |
| `call.targets` | `{call_addr: target_addr}` verified as current call destinations, including calls that need no type adjustment |
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
| `analysis.limits.maxFunctionAnalysisTime` | `1800000` ms (30 minutes) |
| `analysis.limits.maxFunctionUpdateCount` | `1024` |
| `analysis.outlining.builtins` | `false` |

# Make deflatten cleanup plan-owned and atomic

Deflatten state-write cleanup will be derived from the same current-MLIL analysis
that proves each recovered transition. Every redirection plan carries an
`obsolete_state_writes` set of exact instruction indices. Target proof and
cleanup proof are independent: an uncertain target produces no plan, while a
proved target with uncertain cleanup remains a valid plan with an empty set.
Matching a state variable, token value, or low token bits elsewhere in the
function is not cleanup evidence.

An unconditional plan contains all private `exit_jumps` from its original
region. Replaying its concrete `(state_token, width)` from every dispatcher entry
must reach one common target. Dispatcher replay evaluates variable/constant
`MLIL_CMP_E`, `MLIL_CMP_NE`, and signed or unsigned `LT`, `LE`, `GT`, and `GE`
predicates with their original operand order and bitvector width. It does not
perform symbolic range solving; an unsupported comparison, width mismatch, or
ambiguous route rejects the transition.

For a conditional transition, every path in each selected arm must terminate
at a dispatcher entry and must establish the same concrete token; mere
existential reachability or one agreeing write elsewhere in the scope is not
target proof.
Assignments in the arms must belong to the recovered state-selection dependency
chain and must not remain live outside the arm scope. More than one valid
conditional candidate in an original region is ambiguous and rejects that
region. Pointer-based state stores require one complete, unique definition
chain from the store destination to the address of the state variable.
Every definition on that chain must dominate its use.

Concrete replay treats dispatcher pass-through blocks as token-preserving only
when they contain NOP/GOTO operations or direct copies between variables on the
proved state dependency chain. Unrelated assignments, side effects, constant
state replacement, or non-dispatcher observers of derived comparison variables
reject the affected dispatcher. Entry and arm ownership is checked across the
whole rewritten region, not only at its final exit.

Each comparison value must be produced by a unique direct-copy chain inside its
own dispatcher row, ending at the state input shared by the selected rows. The
planner does not accept a comparison temporary merely because some definition
elsewhere ultimately traces to state: such a value can be stale on another
dispatcher entry path. Partial, split, or aliased writes to the state channel,
and `STORE_STRUCT` or other potential pointer writes that cannot be resolved as
one exact state update, reject the transition. `ADDRESS_OF_FIELD` counts as an
address escape everywhere `ADDRESS_OF` does.
Only whole `MLIL_VAR`/`MLIL_VAR_SSA` reads count as exact direct copies;
`VAR_FIELD`, split, and aliased reads are tracked conservatively as observers or
possible aliases but never substituted for the whole value. Read proofs include
Binary Ninja's explicit `vars_read` metadata.
Variable worklists, bindings, and de-duplication use Binary Ninja variable
equality/identity, never `str` or `repr`: two different storage objects may have
the same display name. An auxiliary/non-dominant comparison block joins the
dispatcher boundary only after its complete routing prefix passes the same
purity proof as a selected row; an impure IF block is not hidden from observer
analysis merely because its comparison traces to state.

When an `MLIL_IF` condition is a predicate variable, the planner maps its SSA
definition through `non_ssa_form`, verifies the exact current non-SSA
instruction, and accepts it only when that definition occurs earlier in the
same dispatcher row. The state copy chain must precede that comparison,
not merely the later IF use. Calls, tail calls, syscalls, or intrinsics receiving
a possible state pointer invalidate target proof because they may replace the
otherwise concrete token. A complete zero-offset pointer copy may be proved as
an exact state store only when every known copy width agrees; field values,
truncating copies, nonzero arithmetic, and otherwise ambiguous arithmetic remain
fail-closed possible mutations. Possible-address traversal follows all available
field/split/aliased definitions without a fixed depth cutoff. If the state
address has been stored into memory or retained by an unknown operation,
including indirectly through `holder = &state; call(&holder)`, later unknown
memory effects or non-exact stores invalidate token proof even without an
explicit pointer argument. Traps and breakpoints are unknown effects when an
address has escaped. `MLIL_UNIMPL` and `MLIL_UNIMPL_MEM` reject the transition
unconditionally because their state semantics cannot be proved.

`rewrite_redirections_mlil` validates and applies every selected exit rewrite or
conditional rewrite together with every exact state-write NOP in one MLIL
copy-transform. If any selected instruction is missing, has an unsupported
operation, conflicts with another replacement, or fails to copy, the complete
replacement is discarded. A plan's source operation, expression identity, and
address must still match the instruction at that index in current MLIL before
copying. Rewrite and cleanup indices must be non-negative exact `int` values
(not booleans), the current instruction must report the same index, and every
target basic-block start obeys the same exact-integer rule. This preserves CFG
recovery when cleanup is merely uncertain without permitting a partial graph
mutation.

Cleanup ownership is stricter than target proof. An incoming edge from outside
the rewritten arm or region rejects a conditional plan because the shared exit
mutation would also affect the foreign path. For unconditional plans it merely
leaves `obsolete_state_writes` empty when the owned exit itself remains valid.
Conditional plans use an exit-preserving rewrite when each private arm reaches
a distinct GOTO directly into a dispatcher comparison row. Otherwise the planner
may shortcut the original IF only when all skipped state-channel work is proved
dispatcher-only and privately owned; cleanup uncertainty then rejects that
shortcut rather than silently bypassing the writes.

A partial/split/aliased state mutation anywhere that may consume preserved bits
also blocks cleanup of earlier state writes. The edge plan may remain valid when
it preserves execution, but its cleanup set stays empty; cleanup safety is not
inferred from a later full-write pattern.

The separate deflatten Cleanup workflow activity, function-wide NOP scan, and
`dispatchthis_state_consts` / `dispatchthis_state_vars` view-level maps are
removed. `dispatchthis_mlil_stable` remains only as the cross-function string
decrypt gate: workflow clears the current function's marker before a deflatten
attempt and publishes it only after installing the atomic replacement. Binary
Ninja reanalysis may erase the MLIL overlay, so plans and exact cleanup evidence
are recomputed from the current MLIL on later workflow runs.

Branch-target and call-target phase cleanup are unchanged. They remain narrowly
rooted in their own recovery facts and do not remove deflatten state writes.

# ppsp patterns

## Singleton entry trampoline

- Slot: `branch_targets`
- Observed: `ppsp c0714`, `main`, `0x97e5e4`
- Meaning: restores the one executable successor of the entry trampoline.
- Required proof: a current non-constant `LLIL_JUMP`, current LLIL SSA, an initialized-data snapshot, and `evaluate_values` returning exactly one current executable target. The observed value case has no CFG path edges; no branch direction is inferred.
- Safe rejection: missing SSA or data snapshot returns `Inconclusive`; incomplete, multi-target, malformed, or non-executable results emit no fact.
- Test: `tests/test_branch_targets.py`

```text
0x97e5e4: LLIL_JUMP jump(x8#14)
x8#14 = [x8#13].q @ mem#16
[x22#2 + 0x3568 {var_108}].d = x8#10.w8 @ mem#15 -> mem#16
CompleteValues(values=(0x986a24,), PathSource(edges=()))
```

## Literal table trampoline

- Slot: `branch_targets`
- Observed after the entry successor is installed in `ppsp c0714` `main`.
- Meaning: a current raw LLIL block reconstructs a literal table pointer through exact stack-local writes, then loads one executable pointer and jumps to it.
- Required proof: the destination is the last local register write before the jump; all setup instructions are literal-preserving `SET_REG`/`STORE` operations (with only a direct no-argument call permitted); the table pointer is reconstructed from the current block; and the loaded pointer is executable in the initialized-data snapshot.
- Safe rejection: aliasing stack writes, unknown calls, non-local memory, malformed instruction order, or a non-executable pointer emit no fact.

The provider does not match a table address or source address. The observed addresses are audit evidence only.

## Boolean selector table

- Slot: `branch_targets`
- Observed: subsequent `main` dispatch blocks use `BOOL_TO_INT(CMP_E|CMP_SLE)`, `AND 1`, a stack-local selector store/load, sign extension, pointer-sized table stride, static pointer load, and an indirect jump.
- Meaning: restores the complete static target set and, when the two table entries differ, preserves the exact condition direction: `BOOL_TO_INT(condition)` selects slot zero on false and slot one on true. Equal entries remain an unconditional singleton fact.
- Required proof: the ten current tail instructions are contiguous in one basic block and have the exact data-flow shape; the selector store/load evaluate to the same direct stack offset; both contiguous pointer-sized entries are initialized executable targets; and the nested selector is a supported direct comparison. The fact retains the current raw or user-informed `LLIL_JUMP_TO` plus its enclosing `LLIL_BOOL_TO_INT` as witnesses: the enclosing expression is the stable condition root, while its comparison preserves the exact arm direction. A normal table-base prefix is replayed only along one unique current `LLIL_JUMP_TO` route. A complete literal-only prefix before the first routing jump is snapshotted only for that current query and copied into each route proof.
- Safe rejection: an unsupported comparison, malformed tail, invalid target-map index, non-current block, unknown prefix effect, non-executable table entry, pointer overflow, or any shape mismatch emits no fact. If unique route replay has a non-empty but incomplete state, the frozen fallback is not used.

## Route-normalized dispatcher prefix

- Slot: proof support for `Boolean selector table`, `Frozen-prefix boolean selector table`, and `Frozen frame table tail`; it does not itself emit a target fact.
- Observed: `main` has a fully modeled literal setup prefix before its first current `LLIL_JUMP_TO` (current LLIL index `5871` during the captured analysis). Later dispatch blocks are connected by user-informed `LLIL_JUMP_TO` target maps rather than by a single linear instruction stream.
- Meaning: the prefix establishes a current frame/register snapshot that may be replayed into a later table-load block only when exactly one current routing path reaches that block. Its literal snapshot can also support the separate frozen selector proof below; route ambiguity never changes the unique-route replay rule.
- Required proof: every target-map entry names an exact current basic-block start, even when Binary Ninja maps the basic-block machine address before its first visible LLIL instruction; reverse reachability leaves exactly one route to the stop block; and every traversed instruction preserves the literal/stack model.
- Safe rejection: a non-current index, malformed target map, zero or multiple candidate routes, backward route, or unmodeled instruction abandons the proof. The snapshot is invocation-local and is never reused after reanalysis.

## Frozen-prefix boolean selector table

- Slot: `branch_targets`
- Observed: after UIDF has exposed later flattened CFG, 128 `CMP_SLE`, 56 `CMP_E`, and 896 duplicate-entry `CMP_E` selector tails have multiple current `LLIL_JUMP_TO` routes to their table-load block. Their prefix is complete before the first route jump, so normal unique-route replay correctly returns no state.
- Meaning: this is not an ambiguous-CFG choice. It restores the current table target set only from frozen prefix values plus independently stable direct frame bases; when the table entries differ, it also preserves the selector's proven false/true slot direction. No route is selected.
- Required proof: exact current one-block boolean selector tail; selector store/load share a stable direct base and exact offset; exactly the modeled selector STORE overlaps that slot after the prefix; table-pointer load has a stable direct base; prefix evaluation gives its pointer-sized value; all later STOREs are statically addressed and disjoint from the table slot; later calls/intrinsics meet the frozen-prefix safety policy; both static entries are initialized executable pointers. The result is sorted and de-duplicated.
- Safe rejection: an available but incomplete unique route state, changed base, extra selector-slot write, table-slot overlap, unknown effect, pointer overflow, malformed tail, unsupported predicate, or non-executable entry emits no fact.
- Test: `tests/test_branch_targets.py::test_branch_targets_recovers_a_frozen_boolean_selector_after_ambiguous_routes`, plus the comparison, extra-store, and duplicate-entry regressions.

## SIMD opaque-predicate decoy

- Slot: a preservation boundary while proving the routing-prefix/frame state; it is not a selector decoder.
- Observed: a subset of the five late raw jumps runs AArch64 vector lane duplication and vector/XOR work, then stores a value unrelated to the table index/table-base pair that controls the subsequent indirect jump. The remaining tail shares the frozen-frame pair without a local SIMD sequence, so SIMD is never a required selector shape.
- Meaning: `vdupq_laneq_s32(reg, const)` is a register-only operation in this sample. It can preserve previously proven stack slots while invalidating volatile register values; the surrounding vector/XOR result must not be promoted into a branch-direction or table-index claim.
- Required proof: the intrinsic has exactly the observed name and register-plus-constant operand form. Any actual target still has to be recovered from the later exact scalar table tail and initialized-data snapshot.
- Safe rejection: every other intrinsic invalidates the stack proof. This includes a similarly named operation with a different operand shape; no general SIMD-memory-effect assumption is made.

## Frozen frame table tail

- Slot: `branch_targets`
- Observed: five late raw `main` jumps share the same frozen-frame table tail; the SIMD opaque-predicate decoy above appears on a subset before the independent signed-selector and table-base loads from `x22` frame slots.
- Meaning: restores the exact static entry established by the modeled pre-route frame prefix; the vector value is not treated as a selector.
- Required proof: both tail loads use the same direct, stable stack-frame base; their exact-width values are present before the first current `LLIL_JUMP_TO`; every later current `STORE` has a statically reconstructed stack address and is disjoint from both slots; all calls are direct/no-argument; and every observed intrinsic is on the register-only allowlist. The table entry itself must be executable in the initialized-data snapshot.
- Safe rejection: a later overlapping write, an unstable base, an unknown store/call/intrinsic, a malformed tail, arithmetic overflow, or a non-executable table entry emits no fact.
- Test: `tests/test_branch_targets.py::test_branch_targets_recovers_a_frozen_frame_table_after_the_first_route_jump`.

## Boundary

The provider is read-only. It registers only `branch_targets`; `workflow.py` remains solely responsible for applying the facts, scheduling reanalysis, receipts, and downstream cleanup.

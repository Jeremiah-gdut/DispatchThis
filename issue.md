# Deferred core issues

## Constant LLIL jump prevents indirect-branch convergence

- Kind: `logic-bug`
- Status: `resolved`
- Evidence: `ppsp c0714`, `main` at `0x978a44`, current source `0x983db0`.
- Minimal current IL/SSA slice:

  ```text
  26823: cond:20 = w9 != 0
  26824: w9 = 0xc0de
  26825: if (cond:20) then 28626 else 28628
  26826: jump(-0xfffffffffd562ccc)

  operation: LLIL_JUMP
  dest.operation: LLIL_CONST_PTR
  ```

  `helpers.llil.iter_indirect_jumps` deliberately excludes this instruction
  because its destination is constant.  In contrast,
  `FunctionWorkflowState.unmapped_unresolved_sources` reads every source from
  Binary Ninja's `unresolved_indirect_branches`, reports `0x983db0`, and
  prevents `branch_stable` from becoming true.

- Why the provider cannot safely solve it: `branch_targets` only receives
  current indirect-jump facts.  A `BranchTargetFact` must carry a concrete
  recovered target, and inventing one for this direct constant branch would
  change its semantics.  The provider cannot remove the core's unmatched
  unresolved marker.
- General core need or bug reproduction: convergence should gate only on the
  unresolved source set represented by current `iter_indirect_jumps` (or an
  equivalent exact LLIL indirect-terminator set), not on constant-destination
  `LLIL_JUMP` instructions that the resolver itself intentionally excludes.
- Proposed fix and required user approval: adjust the workflow/state
  convergence gate to intersect Binary Ninja's unresolved-source set with the
  current indirect-jump source set, and add a regression combining one direct
  `LLIL_CONST_PTR` jump with one resolved indirect jump.
- Resolution: the core now passes the current `iter_indirect_jumps` source set
  through `FunctionWorkflowState.unmapped_unresolved_sources` and every
  branch-stability workflow gate. After GUI restart, `main` at `0x978a44`
  reports `0x983db0` in the global unresolved set but not in the scoped set.

## Reproof failure has no provider-safe disposition for existing user maps

- Kind: `capability-gap`
- Status: `deferred`
- Evidence: `ppsp c0714`, `main` at `0x978a44`.  After current-IL reproof,
  1883 of 1891 user branch sources have receipts.  The remaining eight sources
  are `0x97ea00`, `0x980804`, `0x984d80`, `0x9857e0`, `0x988540`, `0x989630`,
  `0x989e3c`, and `0x993930`.
- Minimal current IL/SSA slice:

  ```text
  w9 = BOOL_TO_INT(CMP_*(...))
  w9 = w9 & 1
  [x19 + selector_offset].d = w9
  x9 = [x19 + table_pointer_offset].q
  w10 = [x19 + selector_offset].d
  x9 = [x9 + sx.q(w10) * 8].q
  jump(x9)

  x19#2 = phi(x19#1, x19#5)
  x19#4 = zx.q(x7#1.w7 + (x25#1.w25 << 0x1d))
  ```

  A current CFG path carries `x19#4` to the tail; the entry operand of
  `x7#1` is undefined.  The provider therefore cannot prove the runtime table
  pointer, even though older user branch metadata exists for the source.

- Why the provider cannot safely solve it: `CompleteBatch` can only add
  positively proved `BranchTargetFact` values.  It has no result that means
  “this pre-existing user map is unproved in the current IL; retain it as an
  explicit unresolved hypothesis” or “retract it under a core-owned policy.”
  The provider must neither call `set_user_indirect_branches` nor silently
  bless the old map.
- General core need or bug reproduction: a workflow-owned disposition is
  needed for user branch metadata that cannot be re-proved after reanalysis.
  It must distinguish a safe unresolved result from an approved exact
  retraction, preserve current-IL witnesses, and keep downstream phases from
  treating unproved edges as stable.

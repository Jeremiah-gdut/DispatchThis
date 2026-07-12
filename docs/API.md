# Helper API

This document describes the public helper modules used by resolver profiles and
passes:

```python
from DispatchThis.helpers import facts, llil, memory, mlil
```

Only names exported by each module's `__all__` are documented here. Underscore
helpers are implementation details.

## `llil`

LLIL helpers inspect low-level IL for indirect branch target recovery.

### Constants

| Name | Purpose |
| --- | --- |
| `U48` | Current bundled branch resolver address mask, `0xffffffffffff`. |
| `CONST_OPERATIONS` | Native BN enums for immediate constants. |
| `INDIRECT_JUMP_OPERATIONS` | Native BN enums for indirect jump/tail-call terminators. |
| `LOAD_OPERATIONS` | Native BN enums for LLIL loads inspected for stack spill/reload constants. |
| `SET_REG_OPERATIONS` | Native BN enums for LLIL SSA register assignments followed by register helpers. |
| `CONST_OPS` | Compatibility names generated from `CONST_OPERATIONS`. |
| `INDIRECT_JUMP_OPS` | Compatibility names generated from `INDIRECT_JUMP_OPERATIONS`. |
| `LOAD_OPS` | Compatibility names generated from `LOAD_OPERATIONS`. |
| `SET_REG_OPS` | Compatibility names generated from `SET_REG_OPERATIONS`. |

LLIL-only code compares the native `*_OPERATIONS` enums. The generated
`*_OPS` names exist only for callers that deliberately combine LLIL and MLIL
selectors.

### `iter_indirect_jumps`

**Signature**

```python
iter_indirect_jumps(llil)
```

**Purpose**

Yield LLIL indirect jump or tail-call terminators whose destination is not
already a constant.

**Parameters**

- `llil`: A Binary Ninja LLIL function-like object. It must be iterable by basic
  block, and each block must be iterable by instruction.

**Returns**

An iterator of LLIL instructions. If `llil` is `None`, the iterator yields
nothing.

**Key behavior and limits**

- Includes operations in `INDIRECT_JUMP_OPERATIONS`.
- Skips instructions whose destination operation is in `CONST_OPERATIONS`.
- Does not resolve targets, rewrite IL, or inspect workflow state.

### `peel_reg_definition`

**Signature**

```python
peel_reg_definition(ssa, expr, trail=None, max_depth=32)
```

**Purpose**

Follow a `LLIL_REG_SSA` expression through simple SSA register definitions until
it reaches a non-register expression or a stop condition.

**Parameters**

- `ssa`: A Binary Ninja LLIL SSA function-like object supporting
  `get_ssa_reg_definition(reg)`.
- `expr`: The starting LLIL expression.
- `trail`: Optional list. When provided, each followed definition is appended in
  traversal order.
- `max_depth`: Maximum number of definition hops.

**Returns**

The peeled LLIL expression. On unresolved definitions, PHI definitions,
unsupported definitions, or Binary Ninja API errors, returns the current
expression rather than raising.

**Key behavior and limits**

- Follows only `LLIL_REG_SSA` expressions.
- Stops at `LLIL_REG_PHI`; use `const_values` when PHI candidate recovery is
  needed.
- Follows only a full `LLIL_SET_REG_SSA`; partial-register writes and every
  other definition shape stop the peel.
- Does not walk CFG paths or choose a PHI edge.

### `const_values`

**Signature**

```python
const_values(bv, ssa, expr, max_depth=32)
```

**Purpose**

Recover every concrete constant candidate the helper can derive for one LLIL
expression.

**Parameters**

- `bv`: BinaryView-like object. It is currently used only by internal helper
  paths that may consult Binary Ninja value information through `ssa`.
- `ssa`: LLIL SSA function-like object supporting register and flag definition
  lookup.
- `expr`: The LLIL expression to evaluate.
- `max_depth`: Maximum recursive expression/definition depth.

**Returns**

A complete `set[int]` of recovered candidates, or `None` when any semantic path
is unknown. Callers must distinguish `None` from a proved candidate set before
checking its cardinality.

**Key behavior and limits**

- Handles LLIL constants, zero/sign/low-part casts, boolean-to-int, shifts, basic
  arithmetic/bitwise operations, register SSA definitions, partial registers,
  stack spill/reload constants, and supported PHI candidate sets.
- `LLIL_BOOL_TO_INT` contributes `{0, 1}` when the predicate cannot be uniquely
  folded.
- PHI nodes contribute candidates only when every non-backedge arm is complete.
  One unknown arm makes the whole result `None`; backedges/cycles are bounded.
- Does not perform CFG path disambiguation or prove which PHI edge is feasible.
  If a profile needs exactly one key, base, or offset, it must first require a
  non-`None` result and then check `len(values) == 1`.
- Arithmetic and casts use each LLIL expression's own bit width. The bundled
  branch gadget applies its 48-bit address mask at the binary-specific formula
  boundary, not inside the general value folder.

### `correlated_const_values`

**Signature**

```python
correlated_const_values(bv, ssa, expr, max_depth=32)
```

**Purpose**

Recover LLIL constant candidates while preserving same-arm relationships across
multiple sibling `LLIL_REG_PHI` nodes in one expression.

**Parameters**

- `bv`: BinaryView-like object, same role as in `const_values`.
- `ssa`: LLIL SSA function-like object supporting register definition lookup.
- `expr`: The LLIL expression to evaluate.
- `max_depth`: Maximum recursive expression/definition depth.

**Returns**

A complete `set[int]` of recovered candidates. `None` from the correlation seam
means there was no multi-PHI case and permits the documented `const_values`
fallback. An empty set means a multi-PHI relation was found but could not be
proved, so the caller must reject it rather than use a Cartesian fallback.

**Key behavior and limits**

- When an expression reads several PHIs at one join, aligns their operands by
  exact predecessor basic block and evaluates the expression once per proven
  predecessor arm.
- Avoids impossible Cartesian-product combinations such as
  `phi(1, 2) + phi(10, 20) -> {11, 12, 21, 22}`; correlated evaluation returns
  `{11, 22}` for that shape.
- Does not replace `const_values`; profiles should use this only when PHI
  operands are expected to come from the same predecessor split.
- Values retain the expression widths established by `const_values`.

### `correlated_phi_values`

**Signature**

```python
correlated_phi_values(ssa, expr, value_func, max_depth=32)
```

**Purpose**

Generic same-arm PHI evaluator for profiles with their own value folder.

**Parameters**

- `ssa`: LLIL SSA function-like object supporting register definition lookup.
- `expr`: The LLIL expression to evaluate.
- `value_func`: Callable with signature `value_func(operand, bindings=None)`.
  It must return a `set[int]`. `bindings` maps the real PHI register objects to
  the arm value selected by the helper; display names are never identity keys.
- `max_depth`: Maximum recursive expression/definition depth when collecting
  PHI registers.

**Returns**

A `set[int]` when same-arm evaluation succeeds, `None` when there is no
multi-PHI case, and an empty set when a detected multi-PHI relationship is
ambiguous or incomplete. Only `None` permits fallback.

**Key behavior and limits**

- Owns only PHI-arm correlation; the caller's `value_func` owns arithmetic,
  loads, width masks, and binary-specific address models.
- Requires every collected PHI to be in the same join block, expose one operand
  for every exact incoming predecessor, and fold each selected operand to one
  complete value.
- Keeps distinct same-named register objects separate.
- Does not mutate IL or workflow state.

### `phi_registers`

```python
phi_registers(ssa, expr, max_depth=32)
```

Return the SSA registers read by `expr` whose definition chains terminate at
`LLIL_REG_PHI`. Structural traversal uses Binary Ninja's `traverse`; only the
definition-chain worklist is project logic.

### `stack_store_sources`

```python
stack_store_sources(ssa, load_expr)
```

Return every exact-width stack-store source that can feed an LLIL load. Stack
slots come from `RegisterValueType.StackFrameOffset`, and store
provenance comes from `get_ssa_memory_definition`. Calls, unknown definitions,
overlapping writes, cycles, or an unresolved PHI arm fail closed. Resolved PHI
arms remain separate candidates; single-value callers accept them only when all
arms fold to the same value.

## `mlil`

MLIL helpers inspect medium-level IL for call targets, global slot analysis,
expression walking, operation queries, concrete dispatcher comparisons, and
cleanup-root collection.

### Constants

| Name | Purpose |
| --- | --- |
| `ADDRESS_OF_OPERATIONS` | Native BN enums accepted as whole-variable or field addresses. |
| `CALL_OPERATIONS` | Native BN enums for typed, untyped, SSA, and tail calls. |
| `CONST_OPERATIONS` | Native BN enums for immediate constants. |
| `LOAD_OPERATIONS` | Native BN enums used by constant folding and load recognition. |
| `LOAD_STRUCT_OPERATIONS` | Native BN enums for struct loads. |
| `SLOT_LOAD_OPERATIONS` | Native BN enums accepted by `load_slot_address`. |
| `SET_VAR_OPERATIONS` | Native BN enums for variable assignments followed by peeling and cleanup helpers. |
| `STORE_OPERATIONS` | Native BN enums inspected by `mlil_stores_to_address`. |
| `ADDRESS_OF_OPS` | Compatibility names generated from `ADDRESS_OF_OPERATIONS`. |
| `CALL_OPS` | Compatibility names generated from `CALL_OPERATIONS`. |
| `CONST_OPS` | Compatibility names generated from `CONST_OPERATIONS`. |
| `LOAD_OPS` | Compatibility names generated from `LOAD_OPERATIONS`. |
| `LOAD_STRUCT_OPS` | Compatibility names generated from `LOAD_STRUCT_OPERATIONS`. |
| `SLOT_LOAD_OPS` | Compatibility names generated from `SLOT_LOAD_OPERATIONS`. |
| `SET_VAR_OPS` | Compatibility names generated from `SET_VAR_OPERATIONS`. |
| `STORE_OPS` | Compatibility names generated from `STORE_OPERATIONS`. |

The `*_OPERATIONS` collections are the normal single-MLIL API. The matching
`*_OPS` collections intentionally contain names because compatibility callers
may combine LLIL and MLIL operations, whose `IntEnum` numeric values can
collide. Every name is generated from Binary Ninja's enums; no operation name is
hand-written in production code.

### `op_name`

**Signature**

```python
op_name(expr)
```

**Purpose**

Return `expr.operation.name`, or `None` when the expression is absent or does
not expose a Binary Ninja operation.

### `same_var`

**Signature**

```python
same_var(left, right)
```

**Purpose**

Compare Binary Ninja variable-like objects only by their real equality/identity.
Display names are deliberately not a fallback: distinct variables may render
with the same name. SSA/aliased wrappers must be normalized explicitly before
calling this helper.

### `var_from_expr`

**Signature**

```python
var_from_expr(expr)
```

**Purpose**

Return the underlying base variable from full, field, SSA-field, or aliased
variable-read forms; otherwise return `None`. This broad helper is for observer
and may-alias analysis, not proof that an expression contains a complete value.

### `direct_var_from_expr`

**Signature**

```python
direct_var_from_expr(expr)
```

**Purpose**

Return the underlying variable only for a whole `MLIL_VAR` or `MLIL_VAR_SSA`
read. Dispatcher copy chains and exact pointer proofs use this narrower helper
so a field, split, or aliased value cannot stand in for the complete state.

### `addressed_var`

**Signature**

```python
addressed_var(expr)
```

**Purpose**

Return the variable named by `MLIL_ADDRESS_OF` or
`MLIL_ADDRESS_OF_FIELD`; otherwise return `None`. This explicit check is needed
because Binary Ninja does not reliably include field-address operations in the
generic address-taken metadata.

### `instruction_writes_variable`

**Signature**

```python
instruction_writes_variable(instruction, variable)
```

**Purpose**

Conservatively detect full, field, split, SSA, or aliased writes to one
variable. The helper combines `vars_written` with explicit operation fields so
`SET_VAR_FIELD`, `SET_VAR_SPLIT`, and `SET_VAR_ALIASED(_FIELD)` cannot be
silently treated as read-only.

### `instruction_reads_variable`

**Signature**

```python
instruction_reads_variable(instruction, variable)
```

**Purpose**

Conservatively detect full, field, split, SSA, or aliased reads of one variable.
The helper combines explicit expression forms with `vars_read`, so observer
proofs do not miss reads that Binary Ninja exposes only as variable operands.

### `expression_may_address_variable`

**Signature**

```python
expression_may_address_variable(mlil, expression, variable)
```

**Purpose**

Conservatively follow expression trees and all available full, field, split,
SSA, or aliased variable definitions to decide whether `ADDRESS_OF` or
`ADDRESS_OF_FIELD` of one variable can reach an expression. Traversal is
cycle-guarded by real variable equality without a fixed depth cutoff; an
incomplete definition query is treated as a possible alias. Taking the address
of a holder also follows that holder's definitions, so `holder = &state;
call(&holder)` is recognized. This is a may-alias guard, not proof that a pointer
is an exact store destination.

### `variable_address_escapes`

**Signature**

```python
variable_address_escapes(mlil, variable)
```

**Purpose**

Return whether an explicit store publishes, or an unknown memory-effecting
operation receives and can retain, a direct or definition-derived address of one
variable. Deflatten planners use this function-level fact so later no-argument
calls or unresolved stores cannot silently recover and mutate dispatcher state.

### `address_escape_checker`

**Signature**

```python
address_escape_checker(mlil)
```

**Purpose**

Build a current-MLIL-scoped escape predicate. Its first query traverses all
explicit stores and unknown memory-effect roots in one shared alias worklist,
then caches semantic base-variable answers. An incomplete definition lookup
makes every answer conservatively true. The predicate must be discarded after
MLIL mutation, finalization, copying, or reanalysis.

### `current_non_ssa_instruction`

**Signature**

```python
current_non_ssa_instruction(mlil, ssa_instruction)
```

**Purpose**

Map an SSA instruction through `non_ssa_form`, require a non-negative exact
instruction index, and verify the operation, expression identity, and address
against current non-SSA MLIL. Return `None` when any identity check fails.

### `has_unknown_memory_effect`

**Signature**

```python
has_unknown_memory_effect(instruction)
```

**Purpose**

Identify call, tail-call, syscall, intrinsic, trap, breakpoint, and unimplemented
memory operations that may mutate memory outside explicit `STORE` handling. A
deflatten planner combines this with `expression_may_address_variable`; passing
a possible state pointer to one of these operations invalidates concrete token
proof.

### `has_unmodeled_semantics`

**Signature**

```python
has_unmodeled_semantics(instruction)
```

**Purpose**

Identify `MLIL_UNIMPL` and `MLIL_UNIMPL_MEM` anywhere in an instruction's
expression tree. Their semantics are unavailable, so a deflatten transition
containing either operation cannot prove a stable state token and must fail
closed even when no state address is known to escape.

### `state_token`

**Signature**

```python
state_token(const_expr, fallback_size=None)
```

**Purpose**

Return a `(value, size_in_bytes)` token from an MLIL constant expression. If the
constant expression has no size and no `fallback_size` is supplied, values that
are negative or wider than 32 bits use size `8`; other values use size `4`.

### `comparison_parts`

**Signature**

```python
comparison_parts(condition)
```

**Purpose**

Parse one supported variable/constant MLIL comparison without losing operand
order, token width, or signedness.

**Parameters**

- `condition`: An MLIL comparison expression.

**Returns**

A dictionary with `op`, `var`, `bound`, and `var_on_left`, or `None` when the
shape or operation is unsupported. `bound` is the normalized
`(value, size_in_bytes)` returned by `state_token`; `var_on_left` records whether
the original expression was `var op constant` rather than `constant op var`.

**Key behavior and limits**

- Supports `MLIL_CMP_E`, `MLIL_CMP_NE`, and signed/unsigned `MLIL_CMP_SLT`,
  `ULT`, `SLE`, `ULE`, `SGE`, `UGE`, `SGT`, and `UGT`.
- Requires one exact whole-variable expression accepted by
  `direct_var_from_expr` and one
  `MLIL_CONST`. It does not follow variable definitions or accept
  variable/variable comparisons.
- Preserves the original operand order; it does not reverse or normalize the
  comparison operator.
- Does not infer ranges or mutate IL.

### `evaluate_comparison`

**Signature**

```python
evaluate_comparison(parts, token)
```

**Purpose**

Evaluate one concrete normalized state token against parsed dispatcher
comparison parts using the comparison's bitvector semantics.

**Parameters**

- `parts`: The dictionary returned by `comparison_parts`.
- `token`: A normalized `(value, size_in_bytes)` state token.

**Returns**

`True` or `False` for a supported same-width comparison, or `None` when the token
and comparison bound have different widths.

**Key behavior and limits**

- Uses `var_on_left` to preserve the original operand order.
- Treats `SLT`, `SLE`, `SGE`, and `SGT` operands as two's-complement signed
  integers at the token width. Unsigned and equality comparisons use the
  normalized masked values.
- Evaluates only the supplied concrete token. It does not solve symbolic
  intervals, choose a CFG edge, or accept malformed `parts` from outside
`comparison_parts`.

### `all_paths_reach_stops`

**Signature**

```python
all_paths_reach_stops(basic_blocks, scope, stop_starts)
```

**Purpose**

Prove that every CFG path within a selected block scope terminates at one of the
given stop blocks.

**Key behavior and limits**

- Uses a least fixed point: a scoped block is proved only after all of its
  successors are stops or already proved blocks.
- Rejects terminal blocks, edges outside both sets, and cycles that admit an
  infinite path, even when another edge can reach a stop.
- Proves termination only; it does not choose or evaluate a dispatcher target.

### `row_local_copy_chain`

**Signature**

```python
row_local_copy_chain(mlil, variable, row, use)
```

**Purpose**

Return the tuple of direct, equal-width variable copies from a dispatcher
comparison variable back to the input variable shared by the comparison rows.
Return `None` when a row-local alias has multiple definitions, an impure source,
a cycle, or a definition at or after the comparison.

The final variable is the dispatcher state channel; its definitions may live in
the original-block regions. Those definitions are token evidence for the
transition planner, not part of the dispatcher row copy chain.

### `all_paths_hit_blocks`

**Signature**

```python
all_paths_hit_blocks(basic_blocks, starts, scope, hit_starts)
```

**Purpose**

Prove that every scoped CFG path from the selected entry blocks executes a
designated hit block before leaving the scope.

**Key behavior and limits**

- Treats a hit block as satisfied on entry because MLIL instructions execute in
  block order before its terminator.
- Uses a least fixed point, so a path that reaches a stop or cycle without a hit
  is not proved.
- Does not inspect the value written in a hit block.

### `dependency_variables`

**Signature**

```python
dependency_variables(mlil, expressions, scope)
```

**Purpose**

Collect variables on definition chains rooted at expressions, following only
definitions inside the selected basic-block scope.

**Key behavior and limits**

- Follows every in-scope definition returned by Binary Ninja and guards cycles.
- Records variables whose definitions lie outside the scope as inputs, without
  following those definitions.
- Does not decide reaching-definition feasibility or prove an expression pure.

### `region_until`

```python
region_until(start_bb, stop_starts)
```

Return the basic-block starts reachable from `start_bb` without entering any
stop block. This shared CFG helper replaces duplicate generic/driver walkers.

### `variables_are_scope_local`

**Signature**

```python
variables_are_scope_local(mlil, variables, scope)
```

**Purpose**

Check that selected variables have no reads or address escapes outside a basic
block scope.

**Key behavior and limits**

- Scans expression trees outside the scope for variable reads and both
  `MLIL_ADDRESS_OF` and `MLIL_ADDRESS_OF_FIELD` uses.
- Is deliberately conservative for non-SSA variables with unrelated uses.
- Does not prove dominance or memory aliasing.

### `scope_locality_checker`

**Signature**

```python
scope_locality_checker(mlil)
```

**Purpose**

Build a current-MLIL-scoped predicate that lazily indexes the basic blocks in
which each semantic base variable is read or has its address taken. Repeated
diamond ownership checks then use set containment instead of rescanning the
whole function. The index is invocation-local and must not cross MLIL mutation
or reanalysis.

### `definitions_cover_all_paths`

**Signature**

```python
definitions_cover_all_paths(mlil, starts, scope, expressions)
```

**Purpose**

Prove that every dependency with an in-scope definition is definitely defined
before each in-scope use on every path from the selected entries.

**Key behavior and limits**

- Computes a forward must-defined set with predecessor intersection.
- Treats variables defined only outside the scope as inputs; a variable with an
  in-scope definition must be established on every relevant path.
- Complements token-value agreement and termination checks; it does not fold
  values or choose reaching definitions by itself.

### `walk_expr`

**Signature**

```python
walk_expr(expr)
```

**Purpose**

Return the expression tree rooted at `expr`.

**Parameters**

- `expr`: A Binary Ninja MLIL expression or instruction. Test doubles must
  expose a compatible `traverse` method.

**Returns**

A list of expression nodes. If `expr` is `None`, returns `[]`.

**Key behavior and limits**

- Delegates structural traversal directly to Binary Ninja's
  `expr.traverse(...)`.

### `expression_has_operation`

**Signature**

```python
expression_has_operation(expr, ops)
```

**Purpose**

Return whether the expression tree rooted at `expr` contains any selected
operation.

**Parameters**

- `expr`: MLIL expression or instruction to inspect.
- `ops`: A Binary Ninja MLIL operation enum, operation name string, or iterable
  of either form.

**Returns**

`True` when any visited node has a matching operation, otherwise `False`.

**Key behavior and limits**

- Uses `walk_expr`.
- Does not follow variable definitions. Use
  `expression_or_definitions_have_operation` when the shape may sit behind
  `MLIL_VAR`.

### `expression_or_definitions_have_operation`

**Signature**

```python
expression_or_definitions_have_operation(mlil, expr, ops, max_depth=16)
```

**Purpose**

Return whether `expr` or any followed `MLIL_VAR` definition contains a selected
operation.

**Parameters**

- `mlil`: MLIL function-like object supporting `get_var_definitions(var)`.
- `expr`: MLIL expression or instruction to inspect.
- `ops`: A Binary Ninja MLIL operation enum, operation name string, or iterable
  of either form.
- `max_depth`: Maximum recursive variable-definition depth.

**Returns**

`True` when any visited expression or followed definition contains a matching
operation, otherwise `False`.

**Key behavior and limits**

- Uses `walk_expr_with_defs`.
- Shares the same definition-following limits as `walk_expr_with_defs`: no CFG
  path reasoning and no feasibility filtering.

### `walk_expr_with_defs`

**Signature**

```python
walk_expr_with_defs(mlil, expr, max_depth=16)
```

**Purpose**

Yield the expression tree rooted at `expr`, and recursively include expressions
behind `MLIL_VAR` definitions.

**Parameters**

- `mlil`: MLIL function-like object supporting `get_var_definitions(var)`.
- `expr`: MLIL expression to inspect.
- `max_depth`: Maximum recursive variable-definition depth.

**Returns**

An iterator of MLIL expression nodes.

**Key behavior and limits**

- Uses `walk_expr` for each visited expression tree.
- Tracks expression object identity and variable identity to avoid cycles.
- Follows every definition returned for a variable, but only through each
  variable once.
- Does not perform CFG path reasoning or decide which definition is feasible.

### `constant_value`

**Signature**

```python
constant_value(mlil, expr)
```

**Purpose**

Recover a direct MLIL constant value after peeling single-definition variables.

**Parameters**

- `mlil`: MLIL function-like object supporting `get_var_definitions(var)`.
- `expr`: MLIL expression to inspect.

**Returns**

The expression's `constant` value, or `None` if the peeled expression is not in
`CONST_OPERATIONS`.

**Key behavior and limits**

- Calls `peel_var_definitions(...)`, whose contract already requires one exact
  whole-variable definition.
- Stops when a variable has zero or multiple definitions.
- Does not evaluate arithmetic, loads, or value-set information. Use
  `fold_constant_value` for broader folding.

### `expression_scalar_value`

**Signature**

```python
expression_scalar_value(mlil, expr)
```

**Purpose**

Recover a direct MLIL constant or Binary Ninja single-value result after peeling
single-definition variables.

**Parameters**

- `mlil`: MLIL function-like object supporting `get_var_definitions(var)`.
- `expr`: MLIL expression to inspect.

**Returns**

An integer value, or `None` when the expression is not a direct constant and does
not expose a Binary Ninja value object of type `ConstantValue`,
`ConstantPointerValue`, or `ImportedAddressValue`.

**Key behavior and limits**

- Calls `peel_var_definitions(...)`.
- Does not evaluate arithmetic, loads, PHI candidates, or memory. Use
  `fold_constant_value` or a profile-private value engine when those semantics
  are required.
- Returns the value as reported by Binary Ninja. It does not apply U48 or U64
  masking.

### `constant_address`

**Signature**

```python
constant_address(mlil, expr, depth=0, max_depth=32, address_mask=None)
```

**Purpose**

Recover a constant address expression, optionally applying a caller-provided
address mask.

**Parameters**

- `mlil`: MLIL function-like object supporting `get_var_definitions(var)`.
- `expr`: MLIL expression to inspect.
- `depth`: Current recursion depth. Callers normally leave this at `0`.
- `max_depth`: Maximum recursive expression/definition depth.
- `address_mask`: Optional integer mask applied to each recovered address result.

**Returns**

An integer address, or `None` when no constant address can be recovered.

**Key behavior and limits**

- Peels variables only when there is exactly one definition.
- Handles direct constants and constant `MLIL_ADD`/`MLIL_SUB` expressions.
- Does not implicitly apply the bundled sample's U48 model. Pass
  `address_mask=U48` at the call site when that model is part of the binary
  formula.

### `load_slot_address`

**Signature**

```python
load_slot_address(mlil, expr, width=8, address_mask=None)
```

**Purpose**

Recover the constant address loaded by an MLIL slot load.

**Parameters**

- `mlil`: MLIL function-like object supporting `get_var_definitions(var)`.
- `expr`: MLIL load expression or variable that peels to a load expression.
- `width`: Required load size in bytes.
- `address_mask`: Optional integer mask applied to the recovered load address.

**Returns**

The recovered slot address, including `MLIL_LOAD_STRUCT` offset when present, or
`None` if the expression is not a matching constant-address load.

**Key behavior and limits**

- Accepts operations in `SLOT_LOAD_OPERATIONS`.
- Requires `expr.size == width`.
- For struct loads, requires `offset` to be an integer.
- Does not implicitly apply U48. Pass `address_mask=U48` explicitly when the
  caller's binary formula requires it.

### `load_slot_offsets`

**Signature**

```python
load_slot_offsets(mlil, expr, width=8, address_mask=None, max_depth=32)
```

**Purpose**

Recover constant slot-load addresses plus constant `MLIL_ADD`/`MLIL_SUB`
offsets around the load.

**Parameters**

- `mlil`: MLIL function-like object supporting `get_var_definitions(var)`.
- `expr`: MLIL expression to inspect.
- `width`: Required slot-load width in bytes.
- `address_mask`: Optional integer mask applied to recovered slot addresses.
- `max_depth`: Maximum recursive expression/definition depth.

**Returns**

A list of `(slot_addr, offset)` tuples. An empty list means no matching slot-load
offset shape was found.

**Key behavior and limits**

- Peels variables with `peel_var_definitions(...)`.
- Uses `load_slot_address` for the base slot load.
- Folds only constant add/sub offsets around the slot load.
- Does not decide whether a slot should become const or whether the resolved
  address is valid.

### `iter_load_slot_offsets`

**Signature**

```python
iter_load_slot_offsets(mlil, width=8, address_mask=None)
```

**Purpose**

Scan an MLIL function for every expression that contains a recoverable slot-load
plus offset.

**Parameters**

- `mlil`: MLIL function-like object exposing `instructions`.
- `width`: Required slot-load width in bytes.
- `address_mask`: Optional integer mask applied to recovered slot addresses.

**Returns**

An iterator of `(expr, use_addr, slot_addr, offset)` tuples.

**Key behavior and limits**

- Walks every instruction with `walk_expr`.
- `use_addr` is the expression address when present, otherwise the containing
  instruction address.
- Delegates recognition to `load_slot_offsets`.
- May yield multiple expressions for the same slot; planners should deduplicate
  at their own semantic level.

### `iter_calls`

**Signature**

```python
iter_calls(mlil, ops=CALL_OPERATIONS)
```

**Purpose**

Yield MLIL call-like instructions from an MLIL function.

**Parameters**

- `mlil`: MLIL function-like object exposing `instructions`.
- `ops`: MLIL operation enum/name or iterable of either form. The default is
  `CALL_OPERATIONS`.

**Returns**

An iterator of MLIL call-like instruction objects.

**Key behavior and limits**

- Scans only top-level MLIL instructions, not nested expressions.
- Does not resolve targets or classify calls as direct/indirect.

### `iter_direct_calls`

**Signature**

```python
iter_direct_calls(mlil)
```

**Purpose**

Yield MLIL call-like instructions whose destination has a recoverable scalar
value.

**Parameters**

- `mlil`: MLIL function-like object exposing `instructions` and, for variable
  destinations, `get_var_definitions(var)`.

**Returns**

An iterator of MLIL call-like instruction objects.

**Key behavior and limits**

- Uses `iter_calls` for traversal and
  `expression_scalar_value(mlil, call.dest)` for target classification.
- Does not validate that the target is executable, typed as a function, or in a
  specific BinaryView section. Profiles still own those binary-specific checks.

### `mlil_stores_to_address`

**Signature**

```python
mlil_stores_to_address(mlil, addr, address_mask=None)
```

**Purpose**

Detect whether an MLIL function contains a store to a constant destination
address.

**Parameters**

- `mlil`: MLIL function-like object exposing `instructions`.
- `addr`: Integer address to match.
- `address_mask`: Optional integer mask passed through to `constant_address`
  when recovering store destinations.

**Returns**

`True` when a matching store is found, otherwise `False`.

**Key behavior and limits**

- Walks every expression below every instruction with `walk_expr`.
- Checks store operations in `STORE_OPERATIONS`.
- Uses `constant_address` on each store destination, so variable peeling is
  single-definition only.
- Does not decide whether a slot should become const; planners still own that
  binary-specific rule.

### `slot_has_no_stores`

**Signature**

```python
slot_has_no_stores(bv, current_mlil, slot_addr, address_mask=None)
```

**Purpose**

Prove, fail-closed, that the current MLIL and every analyzed function referenced
by the slot contain no store to it.

**Returns**

`True` only when every current code reference resolves to a function with
available MLIL and none of those functions stores to the slot. Missing reference
ownership, unavailable MLIL, or reference-query failures return `False`.

### `iter_indirect_calls`

**Signature**

```python
iter_indirect_calls(mlil)
```

**Purpose**

Yield MLIL call instructions whose destination is not already a constant.

**Parameters**

- `mlil`: MLIL function-like object exposing `instructions`.

**Returns**

An iterator of MLIL call instructions. If `mlil` is `None`, yields nothing.

**Key behavior and limits**

- Includes the exact operations in `CALL_OPERATIONS`.
- Skips calls whose destination operation is in `CONST_OPERATIONS`.
- Does not resolve the callee or mutate the call destination.

### `peel_var_definitions`

**Signature**

```python
peel_var_definitions(
    mlil,
    expr,
    trail=None,
    max_depth=64,
)
```

**Purpose**

Follow `MLIL_VAR` expressions through MLIL variable definitions.

**Parameters**

- `mlil`: MLIL function-like object supporting `get_var_definitions(var)`.
- `expr`: Starting MLIL expression.
- `trail`: Optional list. Followed definitions are appended in traversal order.
- `max_depth`: Maximum number of definition hops.

**Returns**

The peeled MLIL expression. On missing, multiple, partial/field definitions,
unsupported definitions, or Binary Ninja API errors, returns the current
expression.

**Key behavior and limits**

- Follows only expressions whose operation is `MLIL_VAR`.
- Requires exactly one whole `MLIL_SET_VAR` definition at each hop.
- Does not perform PHI/path reasoning.

### `fold_constant_value`

**Signature**

```python
fold_constant_value(bv, mlil, expr, depth=0, max_depth=32, load_address_mask=None)
```

**Purpose**

Best-effort fold one MLIL expression to a single integer value for current
call-target style recovery.

**Parameters**

- `bv`: BinaryView-like object supporting `read(addr, size)` for image-memory
  loads.
- `mlil`: MLIL function-like object supporting `get_var_definitions(var)`.
- `expr`: MLIL expression to fold.
- `depth`: Current recursion depth. Callers normally leave this at `0`.
- `max_depth`: Maximum recursive expression/definition depth.
- `load_address_mask`: Optional integer mask applied to addresses before memory
  reads.

**Returns**

An integer value, or `None` when folding fails.

**Key behavior and limits**

- Handles constants, variable definitions, `MLIL_ADD`, `MLIL_SUB`, `MLIL_MUL`,
  zero/sign/low-part casts, MLIL loads, and Binary Ninja value objects of type
  `ConstantValue`, `ConstantPointerValue`, or `ImportedAddressValue`.
- Accepts multiple whole `MLIL_SET_VAR` definitions only when every definition
  folds completely to the same value; otherwise returns `None`.
- Arithmetic and casts are masked to the current expression's Binary Ninja
  width.
- `MLIL_LOAD_STRUCT` includes its field offset before reading.
- Load addresses are not implicitly masked. Pass `load_address_mask=U48` when a
  profile or pass is applying the bundled 48-bit address formula.
- Returns `None` on invalid or short reads through `memory.read_uint_le`.

### `cleanup_roots_for_expr`

**Signature**

```python
cleanup_roots_for_expr(mlil, expr)
```

**Purpose**

Collect instruction indices for variable definitions read by an expression.

**Parameters**

- `mlil`: MLIL function-like object supporting `get_var_definitions(var)`.
- `expr`: MLIL expression to inspect.

**Returns**

A `set[int]` of MLIL instruction indices.

**Key behavior and limits**

- Walks the expression tree with `walk_expr`.
- For every `MLIL_VAR`, adds definition `instr_index` values whose operation is
  in `SET_VAR_OPERATIONS`.
- Returns instruction indices, not expression indices. Cleanup backends map
  SSA/non-SSA forms before final `replace_expr` use.
- Does not decide whether a root is safe to NOP; `phase_cleanup.cleanup_decode`
  owns that liveness check.

### `set_roots_before`

**Signature**

```python
set_roots_before(mlil, site_addrs)
```

**Purpose**

Collect contiguous pure assignment instruction indices immediately before
phase-owned sites.

**Parameters**

- `mlil`: MLIL function-like object exposing `basic_blocks` and indexed
  instruction access.
- `site_addrs`: Iterable of instruction addresses owned by the current phase.

**Returns**

A `set[int]` of assignment instruction indices. Returns an empty set when `mlil`
or `site_addrs` is empty.

**Key behavior and limits**

- Scans each basic block independently.
- For every instruction whose `address` is in `site_addrs`, walks backward in the
  same block and collects contiguous assignments whose operation is in
  `SET_VAR_OPERATIONS`.
- Stops at the first non-assignment.
- Does not inspect dataflow outside the contiguous block prefix.

### `set_roots_before_instruction`

**Signature**

```python
set_roots_before_instruction(mlil, instruction)
```

Collect the same contiguous assignment prefix for one exact current MLIL
instruction. Unlike the address-based receipt helper, this variant never scans
other blocks or instructions that share the same machine address. It returns an
empty set when the instruction cannot be mapped uniquely inside its current
basic block. Branch condition translation uses this exact form after proving the
source `MLIL_IF`; phase cleanup still decides which collected definitions are
actually dead.

## `memory`

Memory helpers perform small BinaryView reads and address checks.

### `read_uint_le`

**Signature**

```python
read_uint_le(bv, addr, width)
```

**Purpose**

Read an unsigned little-endian integer from a BinaryView.

**Parameters**

- `bv`: BinaryView-like object supporting `read(addr, width)`.
- `addr`: Integer address to read.
- `width`: Positive byte width.

**Returns**

The decoded integer, or `None` on normal misses such as invalid reads, `None`
data, or short reads.

**Key behavior and limits**

- Raises `ValueError` when `width <= 0`; invalid helper use should fail loudly.
- Catches BinaryView read exceptions and returns `None`.
- Requires the read to return exactly `width` bytes.

### `read_u8`

**Signature**

```python
read_u8(bv, addr)
```

**Purpose**

Read one unsigned byte.

**Parameters**

- `bv`: BinaryView-like object.
- `addr`: Integer address.

**Returns**

An integer byte value, or `None`.

**Key behavior and limits**

- Delegates to `read_uint_le(bv, addr, 1)`.

### `read_u16le`

**Signature**

```python
read_u16le(bv, addr)
```

**Purpose**

Read a 16-bit little-endian unsigned integer.

**Parameters**

- `bv`: BinaryView-like object.
- `addr`: Integer address.

**Returns**

An integer value, or `None`.

**Key behavior and limits**

- Delegates to `read_uint_le(bv, addr, 2)`.

### `read_u32le`

**Signature**

```python
read_u32le(bv, addr)
```

**Purpose**

Read a 32-bit little-endian unsigned integer.

**Parameters**

- `bv`: BinaryView-like object.
- `addr`: Integer address.

**Returns**

An integer value, or `None`.

**Key behavior and limits**

- Delegates to `read_uint_le(bv, addr, 4)`.

### `read_u64le`

**Signature**

```python
read_u64le(bv, addr)
```

**Purpose**

Read a 64-bit little-endian unsigned integer.

**Parameters**

- `bv`: BinaryView-like object.
- `addr`: Integer address.

**Returns**

An integer value, or `None`.

**Key behavior and limits**

- Delegates to `read_uint_le(bv, addr, 8)`.

### `read_qword_slot`

**Signature**

```python
read_qword_slot(bv, addr)
```

**Purpose**

Read an 8-byte slot value.

**Parameters**

- `bv`: BinaryView-like object.
- `addr`: Integer address.

**Returns**

An integer qword value, or `None`.

**Key behavior and limits**

- Alias for `read_u64le`.
- Does not validate section membership or whether the qword is a pointer.

### `is_mapped_address`

**Signature**

```python
is_mapped_address(bv, addr)
```

**Purpose**

Check whether an address belongs to the BinaryView address space.

**Parameters**

- `bv`: BinaryView-like object supporting `is_valid_offset(addr)`.
- `addr`: Address candidate.

**Returns**

`True` when `addr` is not `None` and `bv.is_valid_offset(addr)` is truthy;
otherwise `False`.

**Key behavior and limits**

- Catches BinaryView exceptions and returns `False`.
- Does not require a symbol or function at the address.

### `is_executable_target`

**Signature**

```python
is_executable_target(bv, addr)
```

**Purpose**

Check whether an address is aligned for the current architecture and Binary
Ninja marks it executable.

**Parameters**

- `bv`: BinaryView-like object supporting `is_offset_executable(addr)` and an
  optional `arch.instr_alignment`.
- `addr`: Address candidate.

**Returns**

`True` or `False`.

**Key behavior and limits**

- Catches BinaryView exceptions and returns `False`.
- Does not accept a merely mapped data address.

### `is_known_callee`

**Signature**

```python
is_known_callee(bv, addr)
```

**Purpose**

Check whether Binary Ninja has code evidence for a concrete callee.

**Parameters**

- `bv`: BinaryView-like object supporting address/executable queries, function
  lookup, and symbol lookup.
- `addr`: Address candidate.

**Returns**

`True` when the address is mapped and has a function, executable mapping, or an
explicit function-like `SymbolType`; otherwise `False`.

**Key behavior and limits**

- A generic `DataSymbol` or `ExternalSymbol` is not callee evidence.
- Catches BinaryView exceptions and returns `False`.

### `sections_at`

**Signature**

```python
sections_at(bv, addr)
```

**Purpose**

Return sections covering an address.

**Parameters**

- `bv`: BinaryView-like object supporting `get_sections_at(addr)`.
- `addr`: Address candidate.

**Returns**

A tuple of section objects. Returns `()` on misses or BinaryView exceptions.

**Key behavior and limits**

- Normalizes falsey BinaryView results to an empty tuple.

### `in_section`

**Signature**

```python
in_section(bv, addr, names)
```

**Purpose**

Check whether an address belongs to one of the named sections.

**Parameters**

- `bv`: BinaryView-like object.
- `addr`: Address candidate.
- `names`: Section name string or iterable of section name strings.

**Returns**

`True` when any section covering `addr` has a matching `name`; otherwise
`False`.

**Key behavior and limits**

- Converts a single string to a one-element set.
- Compares against each section object's `name` attribute.
- Uses `sections_at`, so BinaryView exceptions are treated as no match.

## `facts`

Fact helpers build the dict shapes consumed by workflow and pass backends.

### `MalformedRecoveryFact`

**Signature**

```python
class MalformedRecoveryFact(ValueError)
```

**Purpose**

Signal incorrect helper use when building recovery facts.

**Parameters**

Standard `ValueError` constructor arguments.

**Returns**

An exception instance.

**Key behavior and limits**

- Raised only by fact builders for malformed required fields or invalid iterable
  inputs.
- Shape misses during profile recognition should normally return `[]` or `None`
  before calling a fact builder.

### `branch_fact`

**Signature**

```python
branch_fact(
    source,
    dest_expr_index,
    targets,
    jump_il,
    cleanup_roots=None,
)
```

**Purpose**

Build an indirect branch recovery fact.

**Parameters**

- `source`: Indirect branch instruction address.
- `dest_expr_index`: Current LLIL destination expression index, used only for
  current-LLIL presentation rewrites.
- `targets`: Iterable of target addresses.
- `jump_il`: Required current LLIL instruction witness retained for exact
  mutation-boundary validation.
- `cleanup_roots`: Optional iterable of instruction indices for branch target
  decode cleanup.

**Returns**

A dict with:

- `source`: integer branch address.
- `dest_expr_index`: integer LLIL expression index.
- `targets`: sorted tuple of unique integer targets.
- `cleanup_roots`: set of integer instruction indices, present only when the
  argument is not `None`.
- `jump_il`: supplied current instruction witness.

**Key behavior and limits**

- Raises `MalformedRecoveryFact` when required integer fields are not integers,
  when `targets` is not iterable, or when `targets` is empty.
- `bool` and negative integers are rejected for address/index fields.
- Does not validate target addresses against a BinaryView.
- Does not call `Function.set_user_indirect_branches`; workflow owns that
  mutation.

### `call_fact`

**Signature**

```python
call_fact(
    call_il,
    target,
    decode_def=None,
    cleanup_roots=None,
    call_addr=None,
    cleanup_load_roots=None,
)
```

**Purpose**

Build an indirect call recovery fact.

**Parameters**

- `call_il`: MLIL call instruction object.
- `target`: Concrete callee address.
- `decode_def`: Optional descriptive MLIL definition witness that computed the target;
  it is not a rewrite target.
- `cleanup_roots`: Optional iterable of instruction indices for call target
  decode cleanup. The backend replaces these with the current call's exact SSA
  reaching-definition slice before mutation.
- `call_addr`: Optional call-site address override. When omitted,
  `call_il.address` is used.
- `cleanup_load_roots`: Optional subset of `cleanup_roots` whose current
  assignments contain loads believed to belong to the call-target definition slice;
  the backend independently recomputes this set before mutation.

**Returns**

A dict with:

- `call_il`: original call instruction object.
- `call_addr`: integer call-site address.
- `target`: integer callee address.
- `decode_def`: supplied decode definition or `None`.
- `cleanup_roots`: set of integer instruction indices.
- `cleanup_load_roots`: set of integer instruction indices, present only when
  non-empty.

**Key behavior and limits**

- Raises `MalformedRecoveryFact` when `call_il` is `None`, when `call_addr` is
  missing, negative, or not an integer, when `target` is not a non-negative integer, or when
  `cleanup_roots`/`cleanup_load_roots` are not iterables of integers, or when a
  load root is not a subset of all cleanup roots.
- Does not validate that `target` is a call target.
- Does not grant cleanup authority: plan indices are advisory until rebound from the
  current call's exact SSA reaching definitions.
- Does not apply call type adjustments or rewrite MLIL; workflow and pass
  backends own those actions.

### `global_constant_fact`

**Signature**

```python
global_constant_fact(slot_addr, type_name)
```

**Purpose**

Build a global constant slot recovery fact.

**Parameters**

- `slot_addr`: Address of the global slot.
- `type_name`: Non-empty type name string to apply.

**Returns**

A dict with:

- `slot_addr`: integer slot address.
- `type`: type name string.

**Key behavior and limits**

- Raises `MalformedRecoveryFact` for an invalid slot address or empty/non-string
  `type_name`.
- Does not define a data variable; workflow owns BinaryView mutation and the
  function global-phase receipt.
- Does not validate section, store behavior, or address validity.
- Recognition evidence relevant to the profile, such as raw values, resolved
  addresses, or use sites, is not part of the fact.

### `string_decrypt_fact`

**Signature**

```python
string_decrypt_fact(call_addr, src_addr, dst_addr, plaintext)
```

**Purpose**

Build a string decrypt recovery fact.

**Parameters**

- `call_addr`: Decrypt call-site address.
- `src_addr`: Source encrypted blob address.
- `dst_addr`: Destination buffer address.
- `plaintext`: Recovered bytes as `bytes` or `bytearray`.

**Returns**

A dict with:

- `call_addr`: integer call-site address.
- `src_addr`: integer source address.
- `dst_addr`: integer destination address.
- `plaintext`: immutable `bytes`.

**Key behavior and limits**

- Raises `MalformedRecoveryFact` when address fields are not non-negative integers or
  `plaintext` is not `bytes`/`bytearray`.
- Converts `bytearray` plaintext to `bytes`.
- Does not write comments; the string-decrypt backend owns annotation.

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
| `CONST_OPS` | LLIL operation names treated as immediate constants: `LLIL_CONST`, `LLIL_CONST_PTR`. |
| `INDIRECT_JUMP_OPS` | LLIL terminator operations scanned as indirect control flow: `LLIL_JUMP`, `LLIL_JUMP_TO`, `LLIL_TAILCALL`. |
| `LOAD_OPS` | LLIL load operations that may be inspected for stack spill/reload constants. |
| `SET_REG_OPS` | LLIL SSA register assignment operations followed by register helpers. |

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

- Includes operations named in `INDIRECT_JUMP_OPS`.
- Skips instructions whose `dest.operation.name` is in `CONST_OPS`.
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
- Requires followed definitions to expose `src`.
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

A `set[int]` of recovered candidates. An empty set means no concrete candidate
was recovered.

**Key behavior and limits**

- Handles LLIL constants, zero/sign/low-part casts, boolean-to-int, shifts, basic
  arithmetic/bitwise operations, register SSA definitions, partial registers,
  stack spill/reload constants, and supported PHI candidate sets.
- `LLIL_BOOL_TO_INT` contributes `{0, 1}` when the predicate cannot be uniquely
  folded.
- PHI nodes contribute candidate values from their operands. Backedges/cycles are
  bounded with a seen set.
- Does not perform CFG path disambiguation or prove which PHI edge is feasible.
  If a profile needs exactly one key, base, or offset, it must check
  `len(values) == 1`.
- Values are masked to the helper's current 48-bit LLIL address model.

## `mlil`

MLIL helpers inspect medium-level IL for indirect call targets, global slot
analysis, expression walking, and cleanup-root collection.

### Constants

| Name | Purpose |
| --- | --- |
| `CONST_OPS` | MLIL constant operations: `MLIL_CONST`, `MLIL_CONST_PTR`. |
| `LOAD_OPS` | MLIL load operations used by constant folding. |
| `LOAD_STRUCT_OPS` | Struct load operations supported by slot-address helpers. |
| `SLOT_LOAD_OPS` | Load operations accepted by `load_slot_address`. |
| `SET_VAR_OPS` | MLIL variable assignment operations followed by peeling and cleanup helpers. |
| `STORE_OPS` | MLIL store operations inspected by `mlil_stores_to_address`. |

### `walk_expr`

**Signature**

```python
walk_expr(expr)
```

**Purpose**

Return the expression tree rooted at `expr`.

**Parameters**

- `expr`: A Binary Ninja MLIL expression or instruction. Fake test objects may
  be used if they expose compatible child attributes.

**Returns**

A list of expression nodes. If `expr` is `None`, returns `[]`.

**Key behavior and limits**

- Uses Binary Ninja's `expr.traverse(...)` when available.
- Falls back to recursively visiting common child fields:
  `src`, `dest`, `left`, `right`, `condition`, `params`, `output`,
  `vars_read`, and `vars_written`.
- Uses object identity to avoid repeated fallback visits.

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
`CONST_OPS`.

**Key behavior and limits**

- Calls `peel_var_definitions(..., require_single=True, allowed_ops=None)`.
- Stops when a variable has zero or multiple definitions.
- Does not evaluate arithmetic, loads, or value-set information. Use
  `fold_constant_value` for broader folding.

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

- Accepts operations in `SLOT_LOAD_OPS`.
- Requires `expr.size == width`.
- For struct loads, requires `offset` to be an integer.
- Does not implicitly apply U48. Pass `address_mask=U48` explicitly when the
  caller's binary formula requires it.

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
- Checks store operations named in `STORE_OPS`.
- Uses `constant_address` on each store destination, so variable peeling is
  single-definition only.
- Does not decide whether a slot should become const; planners still own that
  binary-specific rule.

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

- Includes instructions whose operation name starts with `MLIL_CALL`.
- Skips calls whose `dest.operation.name` is in `CONST_OPS`.
- Does not resolve the callee or mutate the call destination.

### `peel_var_definitions`

**Signature**

```python
peel_var_definitions(
    mlil,
    expr,
    trail=None,
    max_depth=64,
    require_single=False,
    allowed_ops=SET_VAR_OPS,
)
```

**Purpose**

Follow `MLIL_VAR` expressions through MLIL variable definitions.

**Parameters**

- `mlil`: MLIL function-like object supporting `get_var_definitions(var)`.
- `expr`: Starting MLIL expression.
- `trail`: Optional list. Followed definitions are appended in traversal order.
- `max_depth`: Maximum number of definition hops.
- `require_single`: When `True`, stops unless the variable has exactly one
  definition.
- `allowed_ops`: Iterable of allowed definition operation names, or `None` to
  allow any definition that exposes `src`.

**Returns**

The peeled MLIL expression. On missing definitions, unsupported definitions,
multiple definitions when `require_single=True`, or Binary Ninja API errors,
returns the current expression.

**Key behavior and limits**

- Follows only expressions whose operation is `MLIL_VAR`.
- Requires each followed definition to expose `src`.
- Default `allowed_ops` is `SET_VAR_OPS`.
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
- Follows the first `SET_VAR_OPS` definition for `MLIL_VAR`, matching the current
  single-callee backend behavior.
- Arithmetic is masked to 64 bits.
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
  in `SET_VAR_OPS`.
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
  `SET_VAR_OPS`.
- Stops at the first non-assignment.
- Does not inspect dataflow outside the contiguous block prefix.

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

### `is_valid_address`

**Signature**

```python
is_valid_address(bv, addr)
```

**Purpose**

Check whether a BinaryView offset is valid.

**Parameters**

- `bv`: BinaryView-like object supporting `is_valid_offset(addr)`.
- `addr`: Address candidate.

**Returns**

`True` when `addr` is not `None` and `bv.is_valid_offset(addr)` is truthy;
otherwise `False`.

**Key behavior and limits**

- Catches BinaryView exceptions and returns `False`.
- Does not require a symbol or function at the address.

### `is_valid_target`

**Signature**

```python
is_valid_target(bv, addr)
```

**Purpose**

Check whether an address is valid as a generic recovered target.

**Parameters**

- `bv`: BinaryView-like object.
- `addr`: Address candidate.

**Returns**

`True` or `False`.

**Key behavior and limits**

- Currently delegates to `is_valid_address`.
- Use `is_call_target` when a callee-like target is required.

### `is_call_target`

**Signature**

```python
is_call_target(bv, addr)
```

**Purpose**

Check whether an address looks like a concrete call target.

**Parameters**

- `bv`: BinaryView-like object supporting `is_valid_offset`, `get_symbol_at`,
  and `get_function_at`.
- `addr`: Address candidate.

**Returns**

`True` when the address is valid and has either a symbol or a function at that
address; otherwise `False`.

**Key behavior and limits**

- Catches BinaryView exceptions and returns `False`.
- Does not inspect calling convention or function type.

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
branch_fact(source, dest_expr_index, targets, newly_resolved=True, cleanup_roots=None)
```

**Purpose**

Build an indirect branch recovery fact.

**Parameters**

- `source`: Indirect branch instruction address.
- `dest_expr_index`: Current LLIL destination expression index, used only for
  current-LLIL presentation rewrites.
- `targets`: Iterable of target addresses.
- `newly_resolved`: Boolean-like marker for compatibility with branch plan
  consumers.
- `cleanup_roots`: Optional iterable of instruction indices for branch target
  decode cleanup.

**Returns**

A dict with:

- `source`: integer branch address.
- `dest_expr_index`: integer LLIL expression index.
- `targets`: sorted tuple of unique integer targets.
- `newly_resolved`: boolean.
- `cleanup_roots`: set of integer instruction indices, present only when the
  argument is not `None`.

**Key behavior and limits**

- Raises `MalformedRecoveryFact` when required integer fields are not integers,
  when `targets` is not iterable, or when `targets` is empty.
- `bool` is rejected for integer fields.
- Does not validate target addresses against a BinaryView.
- Does not call `Function.set_user_indirect_branches`; workflow owns that
  mutation.

### `call_fact`

**Signature**

```python
call_fact(call_il, target, decode_def=None, cleanup_roots=None, call_addr=None)
```

**Purpose**

Build an indirect call recovery fact.

**Parameters**

- `call_il`: MLIL call instruction object.
- `target`: Concrete callee address.
- `decode_def`: Optional MLIL definition instruction that computed the target.
- `cleanup_roots`: Optional iterable of instruction indices for call target
  decode cleanup.
- `call_addr`: Optional call-site address override. When omitted,
  `call_il.address` is used.

**Returns**

A dict with:

- `call_il`: original call instruction object.
- `call_addr`: integer call-site address.
- `target`: integer callee address.
- `decode_def`: supplied decode definition or `None`.
- `cleanup_roots`: set of integer instruction indices.

**Key behavior and limits**

- Raises `MalformedRecoveryFact` when `call_il` is `None`, when `call_addr` is
  missing or not an integer, when `target` is not an integer, or when
  `cleanup_roots` is not an iterable of integers.
- Does not validate that `target` is a call target.
- Does not apply call type adjustments or rewrite MLIL; workflow and pass
  backends own those actions.

### `global_constant_fact`

**Signature**

```python
global_constant_fact(slot_addr, type_name, value, resolved_addr, use_addr)
```

**Purpose**

Build a global constant slot recovery fact.

**Parameters**

- `slot_addr`: Address of the global slot.
- `type_name`: Non-empty type name string to apply.
- `value`: Raw qword value read from the slot.
- `resolved_addr`: Address produced by applying the caller's slot formula.
- `use_addr`: Address of the MLIL use that justified the fact.

**Returns**

A dict with:

- `slot_addr`: integer slot address.
- `type`: type name string.
- `value`: integer raw slot value.
- `resolved_addr`: integer resolved address.
- `use_addr`: integer use address.

**Key behavior and limits**

- Raises `MalformedRecoveryFact` for invalid integer fields or empty/non-string
  `type_name`.
- Does not define a data variable; workflow owns BinaryView mutation and
  receipts.
- Does not validate section, store behavior, or address validity.

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

- Raises `MalformedRecoveryFact` when address fields are not integers or
  `plaintext` is not `bytes`/`bytearray`.
- Converts `bytearray` plaintext to `bytes`.
- Does not write comments; the string-decrypt backend owns annotation.

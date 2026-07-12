"""LLIL profile helpers."""

from collections import deque

from binaryninja import LowLevelILOperation as L, RegisterValueType


U48 = 0xFFFFFFFFFFFF
U64 = 0xFFFFFFFFFFFFFFFF
_CONSTANT_VALUE_TYPES = {
    RegisterValueType.ConstantValue,
    RegisterValueType.ConstantPointerValue,
}


def _names(*operations):
    """Expose legacy operation names without duplicating BN enum spelling."""
    return tuple(operation.name for operation in operations)


CONST_OPERATIONS = (L.LLIL_CONST, L.LLIL_CONST_PTR)
INDIRECT_JUMP_OPERATIONS = (L.LLIL_JUMP, L.LLIL_JUMP_TO, L.LLIL_TAILCALL)
LOAD_OPERATIONS = (L.LLIL_LOAD, L.LLIL_LOAD_SSA)
SET_REG_OPERATIONS = (L.LLIL_SET_REG_SSA,)

# Mixed-IL profile matchers still exchange names because LLIL and MLIL are
# different IntEnums with overlapping integer values. Derive that compatibility
# surface from BN's enums in one place; all LLIL-only code compares enums.
CONST_OPS = _names(*CONST_OPERATIONS)
INDIRECT_JUMP_OPS = _names(*INDIRECT_JUMP_OPERATIONS)
LOAD_OPS = _names(*LOAD_OPERATIONS)
SET_REG_OPS = _names(*SET_REG_OPERATIONS)

_CMP = {
    L.LLIL_CMP_E: lambda a, b: a == b,
    L.LLIL_CMP_NE: lambda a, b: a != b,
    L.LLIL_CMP_SLT: lambda a, b: a < b,
    L.LLIL_CMP_ULT: lambda a, b: a < b,
    L.LLIL_CMP_SLE: lambda a, b: a <= b,
    L.LLIL_CMP_ULE: lambda a, b: a <= b,
    L.LLIL_CMP_SGE: lambda a, b: a >= b,
    L.LLIL_CMP_UGE: lambda a, b: a >= b,
    L.LLIL_CMP_SGT: lambda a, b: a > b,
    L.LLIL_CMP_UGT: lambda a, b: a > b,
}
_SIGNED_CMP_OPS = {
    L.LLIL_CMP_SLT,
    L.LLIL_CMP_SLE,
    L.LLIL_CMP_SGE,
    L.LLIL_CMP_SGT,
}

_BACKEDGE = object()


def _expression_key(expr):
    expr_index = getattr(expr, "expr_index", None)
    instr_index = getattr(expr, "instr_index", None)
    if expr_index is None and instr_index is None:
        return ("object", id(expr))
    return ("il", id(getattr(expr, "function", None)), expr_index, instr_index)


def iter_indirect_jumps(llil):
    """Yield unresolved LLIL jump/tailcall terminators."""
    if llil is None:
        return
    for block in llil:
        for insn in block:
            if insn.operation not in INDIRECT_JUMP_OPERATIONS:
                continue
            if insn.dest.operation in CONST_OPERATIONS:
                continue
            yield insn


def peel_reg_definition(ssa, expr, trail=None, max_depth=32):
    """Follow complete REG_SSA definitions through simple copies."""
    depth = 0
    while expr is not None and expr.operation == L.LLIL_REG_SSA and depth < max_depth:
        try:
            definition = ssa.get_ssa_reg_definition(expr.src)
        except Exception:  # noqa: BLE001
            return expr
        if definition is None or definition.operation == L.LLIL_REG_PHI:
            return expr
        source = _full_reg_source(definition)
        if source is None:
            return expr
        if trail is not None:
            trail.append(definition)
        expr = source
        depth += 1
    return expr


def const_values(bv, ssa, expr, max_depth=32):
    """Return a complete constant set, or ``None`` when any path is unknown."""
    values = _const_values(bv, ssa, expr, 0, max_depth, set())
    return None if values is _BACKEDGE else values


def correlated_const_values(bv, ssa, expr, max_depth=32):
    """Return constants while preserving same-arm values across sibling PHIs."""
    values = correlated_phi_values(
        ssa,
        expr,
        lambda operand, bindings=None: _const_values_for_operand(
            bv,
            ssa,
            operand,
            0,
            max_depth,
            set(),
            bindings,
        ),
        max_depth=max_depth,
    )
    return const_values(bv, ssa, expr, max_depth=max_depth) if values is None else values


def correlated_phi_values(ssa, expr, value_func, max_depth=32):
    """Evaluate ``expr`` once per proven shared predecessor arm.

    ``value_func(operand, bindings)`` must return a ``set[int]``. The helper
    returns ``None`` when there is no multi-PHI case, an empty set when a
    multi-PHI relationship is ambiguous, and values only after the PHIs are in
    one join block and their operands can be aligned by exact predecessor block.
    """
    phi_regs = tuple(phi_registers(ssa, expr, max_depth=max_depth))
    if len(phi_regs) <= 1:
        return None

    phi_defs = [(reg, _reg_definition(ssa, reg)) for reg in phi_regs]
    if any(
        getattr(definition, "operation", None) != L.LLIL_REG_PHI
        for _reg, definition in phi_defs
    ):
        return set()

    aligned = [_phi_operands_by_predecessor(ssa, definition) for _reg, definition in phi_defs]
    if any(item is None for item in aligned):
        return set()
    join_starts = {item[0] for item in aligned}
    predecessor_sets = {frozenset(item[1]) for item in aligned}
    if len(join_starts) != 1 or len(predecessor_sets) != 1:
        return set()
    predecessors = next(iter(predecessor_sets))

    out = set()
    for predecessor in sorted(predecessors):
        bindings = {}
        for (reg, _definition), (_join_start, operands) in zip(phi_defs, aligned):
            values = value_func(operands[predecessor], None)
            if values is None or values is _BACKEDGE or len(values) != 1:
                return set()
            value = next(iter(values))
            bindings[reg] = value
        arm_values = value_func(expr, bindings)
        if not isinstance(arm_values, set):
            return set()
        out.update(arm_values)
    return out


def _phi_operands_by_predecessor(ssa, phi):
    join = getattr(phi, "il_basic_block", None)
    if join is None:
        return None
    expected = {
        edge.source.start
        for edge in getattr(join, "incoming_edges", ()) or ()
    }
    if not expected:
        return None

    operands = {}
    for operand in getattr(phi, "src", ()) or ():
        definition = _reg_definition(ssa, operand)
        source_block = getattr(definition, "il_basic_block", None)
        source_start = getattr(source_block, "start", None)
        if source_start not in expected or source_start in operands:
            return None
        operands[source_start] = operand
    if set(operands) != expected:
        return None
    return join.start, operands


def _mask_for_expr(expr):
    try:
        size = int(expr.size)
    except Exception:  # noqa: BLE001
        return U64
    if size <= 0:
        return U64
    return (1 << min(size * 8, 64)) - 1


def _signed_value(value, size):
    bits = min((size or 8) * 8, 64)
    mask = (1 << bits) - 1
    value &= mask
    sign = 1 << (bits - 1)
    return value - (1 << bits) if value & sign else value


def _cast_value(operation, expr, value):
    result = value & _mask_for_expr(expr)
    if operation != L.LLIL_SX:
        return result
    source_size = getattr(getattr(expr, "src", None), "size", None) or 8
    return _signed_value(value, source_size) & _mask_for_expr(expr)


def _expr_constant(expr):
    try:
        rv = expr.value
    except Exception:  # noqa: BLE001
        rv = None
    if rv is not None and rv.type in _CONSTANT_VALUE_TYPES:
        return rv.value & _mask_for_expr(expr)
    return None


def _stack_slot(expr):
    if expr is None:
        return None
    try:
        values = expr.possible_values
    except Exception:  # noqa: BLE001
        return None
    if values.type != RegisterValueType.StackFrameOffset:
        return None
    offset = getattr(values, "offset", None)
    return offset if type(offset) is int else None


def _memory_stack_store_sources(ssa, memory, slot, size, seen):
    if type(memory) is not int or memory in seen:
        return None
    seen.add(memory)
    try:
        definition = ssa.get_ssa_memory_definition(memory)
    except Exception:  # noqa: BLE001
        return None
    if definition is None:
        return None

    op = definition.operation
    if op == L.LLIL_MEM_PHI:
        arms = [
            _memory_stack_store_sources(ssa, incoming, slot, size, seen.copy())
            for incoming in definition.src_memory
        ]
        if not arms or any(not sources for sources in arms):
            return None
        return tuple(source for sources in arms for source in sources)

    if op != L.LLIL_STORE_SSA:
        return None
    store_slot = _stack_slot(definition.dest)
    store_size = getattr(definition, "size", None)
    if store_slot is None or type(store_size) is not int or store_size <= 0:
        return None
    if store_slot == slot:
        return (definition.src,) if store_size == size else None
    if max(slot, store_slot) < min(slot + size, store_slot + store_size):
        return None
    return _memory_stack_store_sources(ssa, definition.src_memory, slot, size, seen)


def stack_store_sources(ssa, load_expr):
    """Return every exact stack STORE source reaching a load, or fail closed."""
    if ssa is None or load_expr is None:
        return None
    if load_expr.operation == L.LLIL_LOAD:
        try:
            load_expr = load_expr.ssa_form
        except Exception:  # noqa: BLE001
            return None
    if load_expr.operation != L.LLIL_LOAD_SSA:
        return None
    slot = _stack_slot(load_expr.src)
    size = getattr(load_expr, "size", None)
    if slot is None or type(size) is not int or size <= 0:
        return None
    return _memory_stack_store_sources(ssa, load_expr.src_memory, slot, size, set())


def _bool_to_int_const(bv, ssa, expr, depth, max_depth):
    try:
        cond = _define_cond(ssa, expr.src)
    except Exception:  # noqa: BLE001
        cond = expr.src
    cmp_fn = _CMP.get(cond.operation)
    if cmp_fn is not None:
        left_const = _single_const(bv, ssa, cond.left, depth + 1, max_depth)
        right_const = _single_const(bv, ssa, cond.right, depth + 1, max_depth)
        if left_const is not None and right_const is not None:
            if cond.operation in _SIGNED_CMP_OPS:
                width = max(
                    getattr(cond.left, "size", None) or 8,
                    getattr(cond.right, "size", None) or 8,
                )
                left_const = _signed_value(left_const, width)
                right_const = _signed_value(right_const, width)
            return 1 if cmp_fn(left_const, right_const) else 0

    value = _expr_constant(expr)
    return None if value is None else (value & 1)


def _single_const(bv, ssa, expr, depth=0, max_depth=48):
    if expr is None or depth > max_depth:
        return None
    op = expr.operation

    if op in CONST_OPERATIONS:
        return expr.constant & _mask_for_expr(expr)

    if op in (L.LLIL_ZX, L.LLIL_SX, L.LLIL_LOW_PART):
        value = _single_const(bv, ssa, expr.src, depth + 1, max_depth)
        return None if value is None else _cast_value(op, expr, value)

    if op == L.LLIL_BOOL_TO_INT:
        return _bool_to_int_const(bv, ssa, expr, depth + 1, max_depth)

    if op in LOAD_OPERATIONS:
        sources = stack_store_sources(ssa, expr)
        if sources:
            values = [
                _single_const(bv, ssa, source, depth + 1, max_depth)
                for source in sources
            ]
            if values[0] is not None and all(value == values[0] for value in values[1:]):
                return values[0]

    if op in (L.LLIL_LSL, L.LLIL_LSR):
        left = _single_const(bv, ssa, expr.left, depth + 1, max_depth)
        right = _single_const(bv, ssa, expr.right, depth + 1, max_depth)
        if left is None or right is None:
            return None
        return (
            (left << right) if op == L.LLIL_LSL else (left >> right)
        ) & _mask_for_expr(expr)

    if op in (L.LLIL_ADD, L.LLIL_SUB, L.LLIL_AND, L.LLIL_OR, L.LLIL_XOR):
        left = _single_const(bv, ssa, expr.left, depth + 1, max_depth)
        right = _single_const(bv, ssa, expr.right, depth + 1, max_depth)
        if left is None or right is None:
            return None
        if op == L.LLIL_ADD:
            return (left + right) & _mask_for_expr(expr)
        if op == L.LLIL_SUB:
            return (left - right) & _mask_for_expr(expr)
        if op == L.LLIL_AND:
            return (left & right) & _mask_for_expr(expr)
        if op == L.LLIL_OR:
            return (left | right) & _mask_for_expr(expr)
        return (left ^ right) & _mask_for_expr(expr)

    if op == L.LLIL_REG_SSA:
        definition = _reg_definition(ssa, expr.src)
        if definition is None:
            return _vsa_const(ssa, expr)
        if definition.operation == L.LLIL_REG_PHI:
            value = _phi_const(bv, ssa, definition, depth + 1, max_depth)
            if value is not None:
                return value
            return _vsa_const(ssa, expr)
        source = _full_reg_source(definition)
        if source is not None:
            return _single_const(bv, ssa, source, depth + 1, max_depth)
        return None

    if op == L.LLIL_REG_SSA_PARTIAL:
        definition = _reg_definition(ssa, expr.full_reg)
        if definition is None:
            return _expr_constant(expr)
        if definition.operation == L.LLIL_REG_PHI:
            value = _phi_const(bv, ssa, definition, depth + 1, max_depth)
            return None if value is None else (value & _mask_for_expr(expr))
        source = _full_reg_source(definition)
        if source is not None:
            value = _single_const(bv, ssa, source, depth + 1, max_depth)
            return None if value is None else (value & _mask_for_expr(expr))
        return _expr_constant(expr)

    return _expr_constant(expr)


def _bound_value(bindings, reg):
    if not bindings:
        return None
    return bindings.get(reg)


def _const_values_for_operand(bv, ssa, operand, depth, max_depth, seen, bindings=None):
    if hasattr(operand, "operation"):
        return _const_values(bv, ssa, operand, depth, max_depth, seen, bindings)
    definition = _reg_definition(ssa, operand)
    if definition is None:
        return None
    source = _full_reg_source(definition)
    if source is not None:
        return _const_values(bv, ssa, source, depth + 1, max_depth, seen, bindings)
    return _const_values(bv, ssa, definition, depth + 1, max_depth, seen, bindings)


def _const_values(bv, ssa, expr, depth, max_depth, seen, bindings=None):
    if expr is None or depth > max_depth:
        return None
    op = expr.operation

    if op in CONST_OPERATIONS:
        return {expr.constant & _mask_for_expr(expr)}

    if op in (L.LLIL_ZX, L.LLIL_SX, L.LLIL_LOW_PART):
        values = _const_values(bv, ssa, expr.src, depth + 1, max_depth, seen, bindings)
        if not isinstance(values, set):
            return values
        return {_cast_value(op, expr, value) for value in values}

    if op == L.LLIL_BOOL_TO_INT:
        value = _bool_to_int_const(bv, ssa, expr, depth + 1, max_depth)
        return {value} if value is not None else {0, 1}

    if op in LOAD_OPERATIONS:
        sources = stack_store_sources(ssa, expr)
        if sources:
            out = set()
            for source in sources:
                values = _const_values(
                    bv,
                    ssa,
                    source,
                    depth + 1,
                    max_depth,
                    seen.copy(),
                    bindings,
                )
                if not isinstance(values, set):
                    return None
                out.update(values)
            return out or None

    if op in (L.LLIL_LSL, L.LLIL_LSR):
        lefts = _const_values(
            bv, ssa, expr.left, depth + 1, max_depth, seen.copy(), bindings
        )
        rights = _const_values(
            bv, ssa, expr.right, depth + 1, max_depth, seen.copy(), bindings
        )
        if not isinstance(lefts, set) or not isinstance(rights, set):
            return None
        mask = _mask_for_expr(expr)
        return {
            ((left << right) if op == L.LLIL_LSL else (left >> right)) & mask
            for left in lefts
            for right in rights
        }

    if op in (L.LLIL_ADD, L.LLIL_SUB, L.LLIL_AND, L.LLIL_OR, L.LLIL_XOR):
        lefts = _const_values(
            bv, ssa, expr.left, depth + 1, max_depth, seen.copy(), bindings
        )
        rights = _const_values(
            bv, ssa, expr.right, depth + 1, max_depth, seen.copy(), bindings
        )
        if op == L.LLIL_AND:
            if lefts is None and isinstance(rights, set) and len(rights) == 1:
                return _small_mask_values(next(iter(rights)))
            if rights is None and isinstance(lefts, set) and len(lefts) == 1:
                return _small_mask_values(next(iter(lefts)))
        if not isinstance(lefts, set) or not isinstance(rights, set):
            return None
        mask = _mask_for_expr(expr)
        out = set()
        for left in lefts:
            for right in rights:
                if op == L.LLIL_ADD:
                    out.add((left + right) & mask)
                elif op == L.LLIL_SUB:
                    out.add((left - right) & mask)
                elif op == L.LLIL_AND:
                    out.add((left & right) & mask)
                elif op == L.LLIL_OR:
                    out.add((left | right) & mask)
                else:
                    out.add((left ^ right) & mask)
        return out

    if op == L.LLIL_REG_SSA:
        bound = _bound_value(bindings, expr.src)
        if bound is not None:
            return {bound & _mask_for_expr(expr)}
        key = ("reg", expr.src)
        if key in seen:
            return _BACKEDGE
        seen.add(key)
        definition = _reg_definition(ssa, expr.src)
        if definition is None:
            value = _vsa_const(ssa, expr)
            return None if value is None else {value}
        if definition.operation == L.LLIL_REG_PHI:
            return _phi_candidate_values(
                bv,
                ssa,
                definition,
                depth,
                max_depth,
                seen,
                bindings,
            )
        source = _full_reg_source(definition)
        if source is not None:
            return _const_values(bv, ssa, source, depth + 1, max_depth, seen, bindings)

    if op == L.LLIL_REG_SSA_PARTIAL:
        bound = _bound_value(bindings, expr.full_reg)
        if bound is not None:
            return {bound & _mask_for_expr(expr)}
        key = ("partial", expr.full_reg, expr.src)
        if key in seen:
            return _BACKEDGE
        seen.add(key)
        definition = _reg_definition(ssa, expr.full_reg)
        if definition is None:
            value = _expr_constant(expr)
            return None if value is None else {value & _mask_for_expr(expr)}
        mask = _mask_for_expr(expr)
        if definition.operation == L.LLIL_REG_PHI:
            values = _phi_candidate_values(
                bv,
                ssa,
                definition,
                depth,
                max_depth,
                seen,
                bindings,
            )
            return None if values is None else {value & mask for value in values}
        source = _full_reg_source(definition)
        if source is not None:
            values = _const_values(
                bv,
                ssa,
                source,
                depth + 1,
                max_depth,
                seen,
                bindings,
            )
            if not isinstance(values, set):
                return None
            return {value & mask for value in values}

    value = _single_const(bv, ssa, expr)
    return None if value is None else {value}


def _phi_candidate_values(bv, ssa, phi, depth, max_depth, seen, bindings):
    values = set()
    for variable in phi.src:
        source = _full_reg_source(_reg_definition(ssa, variable))
        if source is None:
            return None
        candidate_values = _const_values(
            bv,
            ssa,
            source,
            depth + 1,
            max_depth,
            seen.copy(),
            bindings,
        )
        if candidate_values is _BACKEDGE:
            continue
        if not isinstance(candidate_values, set):
            return None
        values.update(candidate_values)
    return values or None


def _small_mask_values(mask):
    bits = [1 << bit for bit in range(mask.bit_length()) if mask & (1 << bit)]
    if len(bits) > 8:
        return None  # ponytail: bounded expansion; widen only if samples need bigger runtime indices.
    values = {0}
    for bit in bits:
        values |= {value | bit for value in values}
    return values


def phi_registers(ssa, expr, max_depth=32):
    """Return SSA registers whose definition chains terminate at REG_PHI."""
    out = set()
    seen = set()
    queue = deque(((expr, 0),))
    while queue:
        current, depth = queue.popleft()
        if current is None or depth > max_depth:
            continue
        for node in current.traverse(lambda item: item):
            key = _expression_key(node)
            if key in seen:
                continue
            seen.add(key)
            operation = node.operation
            if operation == L.LLIL_REG_SSA:
                register = node.src
            elif operation == L.LLIL_REG_SSA_PARTIAL:
                register = node.full_reg
            else:
                continue
            definition = _reg_definition(ssa, register)
            if (
                getattr(definition, "operation", None)
                == L.LLIL_REG_PHI
            ):
                out.add(register)
            else:
                source = _full_reg_source(definition)
                if source is not None:
                    queue.append((source, depth + 1))
    return out


def _reg_definition(ssa, reg):
    if ssa is None:
        return None
    try:
        return ssa.get_ssa_reg_definition(reg)
    except Exception:  # noqa: BLE001
        return None


def _full_reg_source(definition):
    """Return the RHS only when a definition overwrites the full SSA register."""
    if (
        definition is not None
        and getattr(definition, "operation", None)
        == L.LLIL_SET_REG_SSA
    ):
        return getattr(definition, "src", None)
    return None


def _flag_definition(ssa, flag):
    if ssa is None:
        return None
    try:
        return ssa.get_ssa_flag_definition(flag)
    except Exception:  # noqa: BLE001
        return None


def _vsa_const(ssa, expr):
    if expr.operation != L.LLIL_REG_SSA:
        return None
    try:
        rv = ssa.source_function.get_reg_value_at(expr.address, expr.src.reg)
    except Exception:  # noqa: BLE001
        return None
    if rv is not None and rv.type in _CONSTANT_VALUE_TYPES:
        return rv.value & _mask_for_expr(expr)
    return None


def _phi_const(bv, ssa, phi, depth, max_depth, seen=None):
    if depth > max_depth or phi is None:
        return None
    if seen is None:
        seen = set()
    if phi.instr_index in seen:
        return _BACKEDGE
    seen.add(phi.instr_index)

    value = None
    for var in phi.src:
        operand = _phi_operand(bv, ssa, var, depth + 1, max_depth, seen)
        if operand is _BACKEDGE:
            continue
        if operand is None:
            return None
        if value is None:
            value = operand
        elif operand != value:
            return None
    return value


def _phi_operand(bv, ssa, var, depth, max_depth, seen):
    if depth > max_depth:
        return None
    definition = _reg_definition(ssa, var)
    if definition is None:
        return None
    op = definition.operation
    if op == L.LLIL_REG_PHI:
        if definition.instr_index in seen:
            return _BACKEDGE
        return _phi_const(bv, ssa, definition, depth + 1, max_depth, seen)
    if op in SET_REG_OPERATIONS:
        src = _full_reg_source(definition)
        if src is None:
            return None
        if src.operation == L.LLIL_REG_SSA:
            return _phi_operand(bv, ssa, src.src, depth + 1, max_depth, seen)
        return _single_const(bv, ssa, src, depth + 1, max_depth)
    return None


def _define_cond(ssa, cond):
    op = cond.operation
    if op == L.LLIL_REG_SSA:
        definition = _reg_definition(ssa, cond.src)
        source = _full_reg_source(definition)
        return source if source is not None else cond
    if op == L.LLIL_FLAG_SSA:
        definition = _flag_definition(ssa, cond.src)
        return definition.src if definition is not None else cond
    return cond


__all__ = (
    "CONST_OPERATIONS",
    "CONST_OPS",
    "INDIRECT_JUMP_OPERATIONS",
    "INDIRECT_JUMP_OPS",
    "LOAD_OPERATIONS",
    "LOAD_OPS",
    "SET_REG_OPERATIONS",
    "SET_REG_OPS",
    "U48",
    "correlated_const_values",
    "correlated_phi_values",
    "const_values",
    "iter_indirect_jumps",
    "peel_reg_definition",
    "phi_registers",
    "stack_store_sources",
)

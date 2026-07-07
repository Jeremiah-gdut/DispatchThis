"""LLIL profile helpers."""


U48 = 0xFFFFFFFFFFFF

CONST_OPS = ("LLIL_CONST", "LLIL_CONST_PTR")
INDIRECT_JUMP_OPS = ("LLIL_JUMP", "LLIL_JUMP_TO", "LLIL_TAILCALL")
LOAD_OPS = ("LLIL_LOAD", "LLIL_LOAD_SSA")
SET_REG_OPS = ("LLIL_SET_REG_SSA", "LLIL_SET_REG_SSA_PARTIAL")

_CMP = {
    "LLIL_CMP_E": lambda a, b: a == b,
    "LLIL_CMP_NE": lambda a, b: a != b,
    "LLIL_CMP_SLT": lambda a, b: a < b,
    "LLIL_CMP_ULT": lambda a, b: a < b,
    "LLIL_CMP_SLE": lambda a, b: a <= b,
    "LLIL_CMP_ULE": lambda a, b: a <= b,
    "LLIL_CMP_SGE": lambda a, b: a >= b,
    "LLIL_CMP_UGE": lambda a, b: a >= b,
    "LLIL_CMP_SGT": lambda a, b: a > b,
    "LLIL_CMP_UGT": lambda a, b: a > b,
}

_BACKEDGE = object()


def iter_indirect_jumps(llil):
    """Yield unresolved LLIL jump/tailcall terminators."""
    if llil is None:
        return
    for block in llil:
        for insn in block:
            if insn.operation.name not in INDIRECT_JUMP_OPS:
                continue
            if insn.dest.operation.name in CONST_OPS:
                continue
            yield insn


def peel_reg_definition(ssa, expr, trail=None, max_depth=32):
    """Follow REG_SSA definitions through simple copies, stopping at phis."""
    depth = 0
    while expr is not None and expr.operation.name == "LLIL_REG_SSA" and depth < max_depth:
        try:
            definition = ssa.get_ssa_reg_definition(expr.src)
        except Exception:  # noqa: BLE001
            return expr
        if definition is None or definition.operation.name == "LLIL_REG_PHI":
            return expr
        if not hasattr(definition, "src"):
            return expr
        if trail is not None:
            trail.append(definition)
        expr = definition.src
        depth += 1
    return expr


def const_values(bv, ssa, expr, max_depth=32):
    """Return every concrete LLIL constant candidate for ``expr``."""
    return _const_values(bv, ssa, expr, 0, max_depth, set())


def _mask_for_expr(expr):
    try:
        size = int(expr.size)
    except Exception:  # noqa: BLE001
        return U48
    if size <= 0:
        return U48
    return (1 << min(size * 8, 48)) - 1


def _expr_constant(expr):
    try:
        rv = expr.value
    except Exception:  # noqa: BLE001
        rv = None
    if rv is not None and rv.type.name in ("ConstantValue", "ConstantPointerValue"):
        return rv.value & U48
    return None


def _stack_slot(expr):
    if expr is None or expr.operation.name not in ("LLIL_ADD", "LLIL_SUB"):
        return None

    if expr.operation.name == "LLIL_ADD":
        for reg_expr, const_expr in ((expr.left, expr.right), (expr.right, expr.left)):
            if reg_expr.operation.name != "LLIL_REG_SSA":
                continue
            if const_expr.operation.name not in CONST_OPS:
                continue
            reg = getattr(reg_expr.src, "reg", None)
            if str(reg) in ("sp", "fp"):
                return (str(reg_expr.src), const_expr.constant)

    if expr.operation.name == "LLIL_SUB" and expr.left.operation.name == "LLIL_REG_SSA":
        right = expr.right
        if right.operation.name in CONST_OPS:
            reg = getattr(expr.left.src, "reg", None)
            if str(reg) in ("sp", "fp"):
                return (str(expr.left.src), -right.constant)
    return None


def _stack_store_source(ssa, load_expr):
    if ssa is None or load_expr.operation.name not in LOAD_OPS:
        return None
    slot = _stack_slot(load_expr.src)
    if slot is None:
        return None
    best = None
    best_index = -1
    load_index = getattr(load_expr, "instr_index", 1 << 60)
    for block in ssa:
        for insn in block:
            if insn.operation.name not in ("LLIL_STORE", "LLIL_STORE_SSA"):
                continue
            if getattr(insn, "instr_index", -1) >= load_index:
                continue
            if _stack_slot(insn.dest) != slot:
                continue
            instr_index = getattr(insn, "instr_index", -1)
            if instr_index > best_index:
                best = insn.src
                best_index = instr_index
    return best


def _bool_to_int_const(bv, ssa, expr, depth, max_depth):
    try:
        cond = _define_cond(ssa, expr.src)
    except Exception:  # noqa: BLE001
        cond = expr.src
    cmp_fn = _CMP.get(cond.operation.name)
    if cmp_fn is not None:
        left_const = _single_const(bv, ssa, cond.left, depth + 1, max_depth)
        right_const = _single_const(bv, ssa, cond.right, depth + 1, max_depth)
        if left_const is not None and right_const is not None:
            return 1 if cmp_fn(left_const, right_const) else 0

    value = _expr_constant(expr)
    return None if value is None else (value & 1)


def _single_const(bv, ssa, expr, depth=0, max_depth=48):
    if expr is None or depth > max_depth:
        return None
    op = expr.operation.name

    if op in CONST_OPS:
        return expr.constant & U48

    if op in ("LLIL_ZX", "LLIL_SX", "LLIL_LOW_PART"):
        return _single_const(bv, ssa, expr.src, depth + 1, max_depth)

    if op == "LLIL_BOOL_TO_INT":
        return _bool_to_int_const(bv, ssa, expr, depth + 1, max_depth)

    if op in LOAD_OPS:
        src = _stack_store_source(ssa, expr)
        if src is not None:
            return _single_const(bv, ssa, src, depth + 1, max_depth)

    if op in ("LLIL_LSL", "LLIL_LSR"):
        left = _single_const(bv, ssa, expr.left, depth + 1, max_depth)
        right = _single_const(bv, ssa, expr.right, depth + 1, max_depth)
        if left is None or right is None:
            return None
        return ((left << right) if op == "LLIL_LSL" else (left >> right)) & U48

    if op in ("LLIL_ADD", "LLIL_SUB", "LLIL_AND", "LLIL_OR", "LLIL_XOR"):
        left = _single_const(bv, ssa, expr.left, depth + 1, max_depth)
        right = _single_const(bv, ssa, expr.right, depth + 1, max_depth)
        if left is None or right is None:
            return None
        if op == "LLIL_ADD":
            return (left + right) & U48
        if op == "LLIL_SUB":
            return (left - right) & U48
        if op == "LLIL_AND":
            return (left & right) & U48
        if op == "LLIL_OR":
            return (left | right) & U48
        return (left ^ right) & U48

    if op == "LLIL_REG_SSA":
        definition = _reg_definition(ssa, expr.src)
        if definition is None:
            return _vsa_const(ssa, expr)
        if definition.operation.name == "LLIL_REG_PHI":
            value = _phi_const(bv, ssa, definition, depth + 1, max_depth)
            if value is not None:
                return value
            live = _live_phi_operand(bv, ssa, definition)
            if live is not None:
                live_definition = _reg_definition(ssa, live)
                if live_definition is not None:
                    value = _single_const(bv, ssa, live_definition.src, depth + 1, max_depth)
                    if value is not None:
                        return value
            return _vsa_const(ssa, expr)
        if hasattr(definition, "src"):
            return _single_const(bv, ssa, definition.src, depth + 1, max_depth)
        return None

    if op == "LLIL_REG_SSA_PARTIAL":
        definition = _reg_definition(ssa, expr.full_reg)
        if definition is None:
            return _expr_constant(expr)
        if definition.operation.name == "LLIL_REG_PHI":
            value = _phi_const(bv, ssa, definition, depth + 1, max_depth)
            return None if value is None else (value & _mask_for_expr(expr))
        if hasattr(definition, "src"):
            value = _single_const(bv, ssa, definition.src, depth + 1, max_depth)
            return None if value is None else (value & _mask_for_expr(expr))
        return _expr_constant(expr)

    return _expr_constant(expr)


def _const_values(bv, ssa, expr, depth, max_depth, seen):
    if expr is None or depth > max_depth:
        return set()
    op = expr.operation.name

    if op in CONST_OPS:
        return {expr.constant & U48}

    if op in ("LLIL_ZX", "LLIL_SX", "LLIL_LOW_PART"):
        return _const_values(bv, ssa, expr.src, depth + 1, max_depth, seen)

    if op == "LLIL_BOOL_TO_INT":
        return {0, 1}

    if op in LOAD_OPS:
        src = _stack_store_source(ssa, expr)
        if src is not None:
            return _const_values(bv, ssa, src, depth + 1, max_depth, seen)

    if op in ("LLIL_LSL", "LLIL_LSR"):
        lefts = _const_values(bv, ssa, expr.left, depth + 1, max_depth, seen)
        rights = _const_values(bv, ssa, expr.right, depth + 1, max_depth, seen)
        return {
            ((left << right) if op == "LLIL_LSL" else (left >> right)) & U48
            for left in lefts
            for right in rights
        }

    if op in ("LLIL_ADD", "LLIL_SUB", "LLIL_AND", "LLIL_OR", "LLIL_XOR"):
        lefts = _const_values(bv, ssa, expr.left, depth + 1, max_depth, seen)
        rights = _const_values(bv, ssa, expr.right, depth + 1, max_depth, seen)
        if op == "LLIL_AND":
            if not lefts and len(rights) == 1:
                return _small_mask_values(next(iter(rights)))
            if not rights and len(lefts) == 1:
                return _small_mask_values(next(iter(lefts)))
        out = set()
        for left in lefts:
            for right in rights:
                if op == "LLIL_ADD":
                    out.add((left + right) & U48)
                elif op == "LLIL_SUB":
                    out.add((left - right) & U48)
                elif op == "LLIL_AND":
                    out.add((left & right) & U48)
                elif op == "LLIL_OR":
                    out.add((left | right) & U48)
                else:
                    out.add((left ^ right) & U48)
        return out

    if op == "LLIL_REG_SSA":
        key = ("reg", str(expr.src))
        if key in seen:
            return set()
        seen.add(key)
        definition = _reg_definition(ssa, expr.src)
        if definition is None:
            value = _vsa_const(ssa, expr)
            return set() if value is None else {value}
        if definition.operation.name == "LLIL_REG_PHI":
            value = _single_const(bv, ssa, expr)
            if value is not None:
                return {value}
            vals = set()
            for var in definition.src:
                operand_definition = _reg_definition(ssa, var)
                if operand_definition is not None and hasattr(operand_definition, "src"):
                    vals.update(_const_values(bv, ssa, operand_definition.src, depth + 1, max_depth, seen.copy()))
            if vals:
                return vals
            return set()
        if hasattr(definition, "src"):
            return _const_values(bv, ssa, definition.src, depth + 1, max_depth, seen)

    if op == "LLIL_REG_SSA_PARTIAL":
        key = ("partial", str(expr.full_reg), str(expr.src))
        if key in seen:
            return set()
        seen.add(key)
        definition = _reg_definition(ssa, expr.full_reg)
        if definition is None:
            value = _expr_constant(expr)
            return set() if value is None else {value & _mask_for_expr(expr)}
        mask = _mask_for_expr(expr)
        if definition.operation.name == "LLIL_REG_PHI":
            value = _single_const(bv, ssa, expr)
            if value is not None:
                return {value & mask}
            vals = set()
            for var in definition.src:
                operand_definition = _reg_definition(ssa, var)
                if operand_definition is not None and hasattr(operand_definition, "src"):
                    vals.update(_const_values(bv, ssa, operand_definition.src, depth + 1, max_depth, seen.copy()))
            if vals:
                return {value & mask for value in vals}
            return set()
        if hasattr(definition, "src"):
            return {
                value & mask
                for value in _const_values(bv, ssa, definition.src, depth + 1, max_depth, seen)
            }

    value = _single_const(bv, ssa, expr)
    return set() if value is None else {value}


def _small_mask_values(mask):
    bits = [1 << bit for bit in range(mask.bit_length()) if mask & (1 << bit)]
    if len(bits) > 8:
        return set()  # ponytail: bounded expansion; widen only if samples need bigger runtime indices.
    values = {0}
    for bit in bits:
        values |= {value | bit for value in values}
    return {value & U48 for value in values}


def _reg_definition(ssa, reg):
    if ssa is None:
        return None
    try:
        return ssa.get_ssa_reg_definition(reg)
    except Exception:  # noqa: BLE001
        return None


def _flag_definition(ssa, flag):
    if ssa is None:
        return None
    try:
        return ssa.get_ssa_flag_definition(flag)
    except Exception:  # noqa: BLE001
        return None


def _vsa_const(ssa, expr):
    if expr.operation.name != "LLIL_REG_SSA":
        return None
    try:
        rv = ssa.source_function.get_reg_value_at(expr.address, str(expr.src.reg))
    except Exception:  # noqa: BLE001
        return None
    if rv is not None and rv.type.name in ("ConstantValue", "ConstantPointerValue"):
        return rv.value & U48
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
    op = definition.operation.name
    if op == "LLIL_REG_PHI":
        if definition.instr_index in seen:
            return _BACKEDGE
        return _phi_const(bv, ssa, definition, depth + 1, max_depth, seen)
    if op in SET_REG_OPS:
        src = definition.src
        if src.operation.name == "LLIL_REG_SSA":
            return _phi_operand(bv, ssa, src.src, depth + 1, max_depth, seen)
        return _single_const(bv, ssa, src, depth + 1, max_depth)
    return None


def _define_cond(ssa, cond):
    op = cond.operation.name
    if op == "LLIL_REG_SSA":
        definition = _reg_definition(ssa, cond.src)
        return definition.src if definition is not None else cond
    if op == "LLIL_FLAG_SSA":
        definition = _flag_definition(ssa, cond.src)
        return definition.src if definition is not None else cond
    return cond


def _eval_predicate(bv, ssa, if_instr):
    cond = _define_cond(ssa, if_instr.condition)
    cmp_fn = _CMP.get(cond.operation.name)
    if cmp_fn is None:
        return None
    left_const = _single_const(bv, ssa, cond.left)
    right_const = _single_const(bv, ssa, cond.right)
    if left_const is not None and right_const is not None:
        return cmp_fn(left_const, right_const)

    try:
        right = cond.right.constant
    except AttributeError:
        return None

    left = cond.left
    if left.operation.name in LOAD_OPS and right in (0x9, 0xA):
        return cmp_fn(0, right)

    try:
        if left.operation.name == "LLIL_AND" and left.right.constant == 1 and right == 0:
            return True
    except AttributeError:
        pass

    return None


def _controlling_if(ssa, block):
    for edge in getattr(block, "incoming_edges", ()):
        pred = edge.source
        last = ssa[pred.end - 1]
        if last.operation.name == "LLIL_IF":
            return pred, last
        if last.operation.name == "LLIL_GOTO":
            for pred_edge in pred.incoming_edges:
                pred2 = pred_edge.source
                last2 = ssa[pred2.end - 1]
                if last2.operation.name == "LLIL_IF":
                    return pred2, last2
    return None, None


def _walk_to_pred(start_block, target_block):
    cur = start_block
    seen = set()
    while cur is not None and cur.start not in seen:
        seen.add(cur.start)
        if any(edge.target.start == target_block.start for edge in cur.outgoing_edges):
            return cur
        outs = cur.outgoing_edges
        cur = outs[0].target if len(outs) == 1 else None
    return None


def _live_phi_operand(bv, ssa, phi):
    block = getattr(phi, "il_basic_block", None)
    if block is None:
        return None
    if_block, if_instr = _controlling_if(ssa, block)
    if if_instr is None:
        return None
    truth = _eval_predicate(bv, ssa, if_instr)
    if truth is None:
        return None

    target_idx = if_instr.true if truth else if_instr.false
    target_block = ssa[target_idx].il_basic_block
    if target_block.start == block.start:
        live_pred = if_block
    else:
        live_pred = _walk_to_pred(target_block, block)
    if live_pred is None:
        return None

    live_doms = {b.start for b in live_pred.dominators}
    flow_through = []
    for var in phi.src:
        definition = _reg_definition(ssa, var)
        if definition is None:
            continue
        definition_block = definition.il_basic_block
        if definition_block.start == live_pred.start:
            return var
        if definition_block.start in live_doms:
            flow_through.append(var)
    return flow_through[0] if len(flow_through) == 1 else None


__all__ = (
    "CONST_OPS",
    "INDIRECT_JUMP_OPS",
    "LOAD_OPS",
    "SET_REG_OPS",
    "U48",
    "const_values",
    "iter_indirect_jumps",
    "peel_reg_definition",
)

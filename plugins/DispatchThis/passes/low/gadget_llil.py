"""
LLIL-stage jump gadget resolver that replaces indirect jump expressions
with replace_expr and an associated jump to the original address 
"""


from ...utils.log import log_debug, log_warn, log_error


U48 = 0xFFFFFFFFFFFF
U64 = 0xFFFFFFFFFFFFFFFF
UNRESOLVED_INDIRECT_TAG = "Unresolved Indirect Control Flow"

# Phi addresses we've already warned about, so the dispatcher's giant
# state-variable phi doesn't flood the log once per gadget.
_warned_phi = set()


def clear_resolved_indirect_branch_tags(func):
    seen = set()
    for branch in func.indirect_branches:
        if branch.auto_defined:
            continue
        if branch.source_addr in seen:
            continue
        seen.add(branch.source_addr)
        func.remove_auto_address_tags_of_type(
            branch.source_addr,
            UNRESOLVED_INDIRECT_TAG,
        )


def schedule_resolved_indirect_branch_tag_cleanup(bv, func_start):
    pending = bv.session_data.setdefault("dispatchthis_tag_cleanup_pending", set())
    if func_start in pending:
        return
    pending.add(func_start)

    def clear_after_analysis():
        try:
            func = bv.get_function_at(func_start)
            if func is not None:
                clear_resolved_indirect_branch_tags(func)
        except Exception as e:  # noqa: BLE001
            log_error(f"[gadget-llil] tag cleanup @ {hex(func_start)}: {e}")
        finally:
            pending.discard(func_start)

    bv.add_analysis_completion_event(clear_after_analysis)

# Constant-truth evaluators for the LLIL comparison ops the predicates use.
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
            if const_expr.operation.name not in ("LLIL_CONST", "LLIL_CONST_PTR"):
                continue
            reg = getattr(reg_expr.src, "reg", None)
            if str(reg) in ("sp", "fp"):
                return (str(reg_expr.src), const_expr.constant)

    if expr.operation.name == "LLIL_SUB" and expr.left.operation.name == "LLIL_REG_SSA":
        right = expr.right
        if right.operation.name in ("LLIL_CONST", "LLIL_CONST_PTR"):
            reg = getattr(expr.left.src, "reg", None)
            if str(reg) in ("sp", "fp"):
                return (str(expr.left.src), -right.constant)
    return None


def _stack_store_source(ssa, load_expr):
    if ssa is None:
        return None
    if load_expr.operation.name not in ("LLIL_LOAD", "LLIL_LOAD_SSA"):
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


def _bool_to_int_const(bv, ssa, expr, depth):
    try:
        cond = _define_cond(ssa, expr.src)
    except Exception:  # noqa: BLE001
        cond = expr.src
    cmp_fn = _CMP.get(cond.operation.name)
    if cmp_fn is not None:
        left_const = _reg_const(bv, ssa, cond.left, depth + 1)
        right_const = _reg_const(bv, ssa, cond.right, depth + 1)
        if left_const is not None and right_const is not None:
            return 1 if cmp_fn(left_const, right_const) else 0

    v = _expr_constant(expr)
    return None if v is None else (v & 1)


# --------------------------------------------------------------------------- #
# table decode (closed form)
# --------------------------------------------------------------------------- #

def get_qword_at(bv, addr):
    """Read a little-endian qword from image memory. ``None`` if unmapped."""
    data = bv.read(addr & U48, 8)
    return int.from_bytes(data, "little") if len(data) == 8 else None


def resolve_indirect_jump_addr(bv, slot, entry_offset, table_base_key, key):
    """Decode one target from the relocated jump table.

        table_base = (*slot + table_base_key) mod 2^48
        entry      = *(table_base + entry_offset)
        target     = (entry + key) mod 2^48
    """
    encoded_table_base = get_qword_at(bv, slot)
    if encoded_table_base is None:
        return None
    table_base = (encoded_table_base + table_base_key) & U48
    entry_addr = (table_base + entry_offset) & U48
    encoded_target = get_qword_at(bv, entry_addr)
    if encoded_target is None:
        return None
    return (encoded_target + key) & U48


# --------------------------------------------------------------------------- #
# constant recovery for a single gadget operand
# --------------------------------------------------------------------------- #

def _reg_const(bv, ssa, expr, depth=0):
    """Resolve a gadget *operand* (offset / table_base_key / key) to a constant
    ``int`` masked to 48 bits, or ``None``.

    Walks an SSA expression through plain reg->reg copies, width casts,
    const-only add/sub, and the opaque-predicate'd selecting ``ϕ``. It does *not*
    chase loads or general arithmetic -- the gadget's own arithmetic is unwound
    structurally by ``parse_jump_gadget``, so this only recovers the constant a
    register holds and never wanders into the runtime table/loop chains that the
    old recursive evaluator dead-ended on."""
    if expr is None or depth > 48:
        return None
    op = expr.operation.name

    if op in ("LLIL_CONST", "LLIL_CONST_PTR"):
        return expr.constant & U48

    if op in ("LLIL_ZX", "LLIL_SX", "LLIL_LOW_PART"):
        return _reg_const(bv, ssa, expr.src, depth + 1)

    if op == "LLIL_BOOL_TO_INT":
        return _bool_to_int_const(bv, ssa, expr, depth + 1)

    if op in ("LLIL_LOAD", "LLIL_LOAD_SSA"):
        src = _stack_store_source(ssa, expr)
        if src is not None:
            return _reg_const(bv, ssa, src, depth + 1)

    if op in ("LLIL_LSL", "LLIL_LSR"):
        l = _reg_const(bv, ssa, expr.left, depth + 1)
        r = _reg_const(bv, ssa, expr.right, depth + 1)
        if l is None or r is None:
            return None
        return ((l << r) if op == "LLIL_LSL" else (l >> r)) & U48

    if op in ("LLIL_ADD", "LLIL_SUB", "LLIL_AND", "LLIL_OR", "LLIL_XOR"):
        l = _reg_const(bv, ssa, expr.left, depth + 1)
        r = _reg_const(bv, ssa, expr.right, depth + 1)
        if l is None or r is None:
            return None
        if op == "LLIL_ADD":
            return (l + r) & U48
        if op == "LLIL_SUB":
            return (l - r) & U48
        if op == "LLIL_AND":
            return (l & r) & U48
        if op == "LLIL_OR":
            return (l | r) & U48
        return (l ^ r) & U48

    if op == "LLIL_REG_SSA":
        d = ssa.get_ssa_reg_definition(expr.src)
        if d is None:
            # No SSA def here (entry / loop-invariant) -- ask VSA.
            return _vsa_const(ssa, expr)
        if d.operation.name == "LLIL_REG_PHI":
            # Loop-invariant constant carried by the phi: if every operand
            # (ignoring back-edges to the phi itself) folds to the same
            # constant, that is the value on any path. This resolves the
            # table_base_key/key held in r12/r8 -- each set to a fixed constant and
            # merged by the dispatcher loop into a phi VSA refuses to fold.
            v = _phi_const(bv, ssa, d, depth + 1)
            if v is not None:
                return v
            # Operands genuinely differ (the opaque-predicate'd entry offset):
            # evaluate the predicate controlling the live edge into the phi.
            live = _live_phi_operand(bv, ssa, d)
            if live is not None:
                ld = ssa.get_ssa_reg_definition(live)
                if ld is not None:
                    v = _reg_const(bv, ssa, ld.src, depth + 1)
                    if v is not None:
                        return v
            return _vsa_const(ssa, expr)
        # LLIL_SET_REG_SSA / _PARTIAL -- a copy or materialized constant.
        return _reg_const(bv, ssa, d.src, depth + 1)

    if op == "LLIL_REG_SSA_PARTIAL":
        d = ssa.get_ssa_reg_definition(expr.full_reg)
        if d is None:
            return _expr_constant(expr)
        if d.operation.name == "LLIL_REG_PHI":
            v = _phi_const(bv, ssa, d, depth + 1)
            return None if v is None else (v & _mask_for_expr(expr))
        if hasattr(d, "src"):
            v = _reg_const(bv, ssa, d.src, depth + 1)
            return None if v is None else (v & _mask_for_expr(expr))
        return _expr_constant(expr)

    v = _expr_constant(expr)
    if v is not None:
        return v

    return None


def _reg_consts(bv, ssa, expr, depth=0, seen=None):
    """Resolve an expression to every small constant it may hold."""
    if expr is None or depth > 32:
        return set()
    if seen is None:
        seen = set()
    op = expr.operation.name

    if op in ("LLIL_CONST", "LLIL_CONST_PTR"):
        return {expr.constant & U48}

    if op in ("LLIL_ZX", "LLIL_SX", "LLIL_LOW_PART"):
        return _reg_consts(bv, ssa, expr.src, depth + 1, seen)

    if op == "LLIL_BOOL_TO_INT":
        return {0, 1}

    if op in ("LLIL_LOAD", "LLIL_LOAD_SSA"):
        src = _stack_store_source(ssa, expr)
        if src is not None:
            return _reg_consts(bv, ssa, src, depth + 1, seen)

    if op in ("LLIL_LSL", "LLIL_LSR"):
        lefts = _reg_consts(bv, ssa, expr.left, depth + 1, seen)
        rights = _reg_consts(bv, ssa, expr.right, depth + 1, seen)
        out = set()
        for l in lefts:
            for r in rights:
                out.add(((l << r) if op == "LLIL_LSL" else (l >> r)) & U48)
        return out

    if op in ("LLIL_ADD", "LLIL_SUB", "LLIL_AND", "LLIL_OR", "LLIL_XOR"):
        lefts = _reg_consts(bv, ssa, expr.left, depth + 1, seen)
        rights = _reg_consts(bv, ssa, expr.right, depth + 1, seen)
        out = set()
        for l in lefts:
            for r in rights:
                if op == "LLIL_ADD":
                    out.add((l + r) & U48)
                elif op == "LLIL_SUB":
                    out.add((l - r) & U48)
                elif op == "LLIL_AND":
                    out.add((l & r) & U48)
                elif op == "LLIL_OR":
                    out.add((l | r) & U48)
                else:
                    out.add((l ^ r) & U48)
        return out

    if op == "LLIL_REG_SSA":
        key = ("reg", str(expr.src))
        if key in seen:
            return set()
        seen.add(key)
        d = ssa.get_ssa_reg_definition(expr.src)
        if d is None:
            v = _vsa_const(ssa, expr)
            return set() if v is None else {v}
        if d.operation.name == "LLIL_REG_PHI":
            vals = set()
            for var in d.src:
                od = ssa.get_ssa_reg_definition(var)
                if od is not None and hasattr(od, "src"):
                    vals.update(_reg_consts(bv, ssa, od.src, depth + 1, seen.copy()))
            if vals:
                return vals
            v = _reg_const(bv, ssa, expr)
            return set() if v is None else {v}
        if hasattr(d, "src"):
            return _reg_consts(bv, ssa, d.src, depth + 1, seen)

    if op == "LLIL_REG_SSA_PARTIAL":
        key = ("partial", str(expr.full_reg), str(expr.src))
        if key in seen:
            return set()
        seen.add(key)
        d = ssa.get_ssa_reg_definition(expr.full_reg)
        if d is None:
            v = _expr_constant(expr)
            return set() if v is None else {v & _mask_for_expr(expr)}
        mask = _mask_for_expr(expr)
        if d.operation.name == "LLIL_REG_PHI":
            vals = set()
            for var in d.src:
                od = ssa.get_ssa_reg_definition(var)
                if od is not None and hasattr(od, "src"):
                    vals.update(_reg_consts(bv, ssa, od.src, depth + 1, seen.copy()))
            if vals:
                return {v & mask for v in vals}
            v = _reg_const(bv, ssa, expr)
            return set() if v is None else {v & mask}
        if hasattr(d, "src"):
            return {v & mask for v in _reg_consts(bv, ssa, d.src, depth + 1, seen)}

    v = _reg_const(bv, ssa, expr)
    return set() if v is None else {v}


def _diag(ssa, expr, depth=0, seen=None):
    """Recursively describe why a register operand won't fold: walk its SSA
    definition through phis and reg->reg copies, showing each step. For
    diagnostics only."""
    pad = "  " * depth
    if expr is None:
        return f"{pad}<none>"
    if expr.operation.name != "LLIL_REG_SSA":
        return f"{pad}{expr.operation.name}: {expr}"
    if seen is None:
        seen = set()
    key = str(expr.src)
    if key in seen or depth > 6:
        return f"{pad}{expr} (cycle/cutoff)"
    seen.add(key)

    d = ssa.get_ssa_reg_definition(expr.src)
    line = f"{pad}{expr} <- {d.operation.name if d else None}: {d}"
    if d is None:
        return line
    if d.operation.name == "LLIL_REG_PHI":
        kid_lines = []
        for var in d.src:
            od = ssa.get_ssa_reg_definition(var)
            if od is not None and od.operation.name == "LLIL_SET_REG_SSA":
                kid_lines.append(_diag(ssa, od.src, depth + 1, seen))
            else:
                kid_lines.append(f"{'  ' * (depth + 1)}{var} <- "
                                 f"{od.operation.name if od else None}: {od}")
        return "\n".join([line] + kid_lines)
    if d.operation.name in ("LLIL_SET_REG_SSA", "LLIL_SET_REG_SSA_PARTIAL"):
        return line + "\n" + _diag(ssa, d.src, depth + 1, seen)
    return line


def _vsa_const(ssa, expr):
    """Last-resort constant recovery via Binary Ninja's value-set analysis.

    The decode gadget holds the table_base_key and key in registers (``r12`` /
    ``r8``) that are loop-invariant constants but whose SSA definition at the
    gadget is the dispatcher's many-predecessor ``ϕ`` -- unfoldable by the
    structural walk and not an opaque-predicate select. VSA computes the constant
    those registers actually carry at this point."""
    if expr.operation.name != "LLIL_REG_SSA":
        return None
    try:
        rv = ssa.source_function.get_reg_value_at(expr.address, str(expr.src.reg))
    except Exception:  # noqa: BLE001
        return None
    if rv is not None and rv.type.name in ("ConstantValue", "ConstantPointerValue"):
        return rv.value & U48
    return None


_BACKEDGE = object()   # sentinel: a phi operand that loops back to a phi in flight


def _phi_const(bv, ssa, phi, depth, seen=None):
    """If every operand of ``phi`` folds to the *same* constant, return it.

    Loop back-edges carry the same value around and are skipped. A back-edge is
    not always the phi referencing itself directly -- it can route through a
    reg->reg copy in another register (e.g. ``r12#8 = r13#46`` where
    ``r13#46 = r12#2``, the phi itself). So operands are followed through copies,
    and any chain that returns to a phi already being walked is treated as a
    back-edge. Used for the loop-invariant table_base_key/key (r12/r8) the
    dispatcher merges into a phi: all real definitions are the identical
    constant, so the phi's value is unambiguous on any path."""
    if depth > 64 or phi is None:
        return None
    if seen is None:
        seen = set()
    if phi.instr_index in seen:
        return _BACKEDGE
    seen.add(phi.instr_index)

    val = None
    for var in phi.src:
        c = _phi_operand(bv, ssa, var, depth + 1, seen)
        if c is _BACKEDGE:
            continue                          # loops back -- same value, skip
        if c is None:
            return None
        if val is None:
            val = c
        elif c != val:
            return None                        # operands disagree -- not invariant
    return val


def _phi_operand(bv, ssa, var, depth, seen):
    """Fold one phi operand to a constant, following reg->reg copies so a
    back-edge routed through another register is detected. Returns an ``int``,
    ``None`` (can't fold), or ``_BACKEDGE`` (chain cycles back to a walked phi)."""
    if depth > 64:
        return None
    d = ssa.get_ssa_reg_definition(var)
    if d is None:
        return None
    op = d.operation.name
    if op == "LLIL_REG_PHI":
        if d.instr_index in seen:
            return _BACKEDGE
        return _phi_const(bv, ssa, d, depth + 1, seen)
    if op in ("LLIL_SET_REG_SSA", "LLIL_SET_REG_SSA_PARTIAL"):
        src = d.src
        # Plain reg->reg copy: keep following so a cycle through it is seen.
        if src.operation.name == "LLIL_REG_SSA":
            return _phi_operand(bv, ssa, src.src, depth + 1, seen)
        return _reg_const(bv, ssa, src, depth + 1)
    return None


def _def_src(ssa, expr, depth=0):
    """Unwrap a ``REG_SSA`` to the expression assigned to it, following plain
    reg->reg copies. Returns the underlying expression (``ADD``/``LOAD``/...) or
    the ``REG_SSA`` itself when it bottoms out at a phi or undefined register."""
    while expr is not None and expr.operation.name == "LLIL_REG_SSA" and depth < 32:
        d = ssa.get_ssa_reg_definition(expr.src)
        if d is None or d.operation.name == "LLIL_REG_PHI":
            return expr
        expr = d.src
        depth += 1
    return expr


# --------------------------------------------------------------------------- #
# opaque-predicate resolution (offset selection)
# --------------------------------------------------------------------------- #

# Constant-truth evaluators for the LLIL comparison ops the predicates use.
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

def _define_cond(ssa, cond):
    """
    Resolve an ``if`` condition through a temp register/flag to the
    underlying comparison expression.
    """
    op = cond.operation.name
    if op == "LLIL_REG_SSA":
        d = ssa.get_ssa_reg_definition(cond.src)
        return d.src if d is not None else cond
    if op == "LLIL_FLAG_SSA":
        d = ssa.get_ssa_flag_definition(cond.src)
        return d.src if d is not None else cond
    return cond


def _eval_predicate(bv, ssa, if_instr):
    """
    Evaluate ``LLIL_IF`` for truth
      1. ``0 OP 0xa``
      2. ``((0 - 1) * 0) & 1 == 0``
    """
    cond = _define_cond(ssa, if_instr.condition)
    cmp_fn = _CMP.get(cond.operation.name)
    if cmp_fn is None:
        return None
    left_const = _reg_const(bv, ssa, cond.left)
    right_const = _reg_const(bv, ssa, cond.right)
    if left_const is not None and right_const is not None:
        return cmp_fn(left_const, right_const)
    try:
        right = cond.right.constant
    except AttributeError:
        return None

    left = cond.left
    if left.operation.name in ("LLIL_LOAD", "LLIL_LOAD_SSA") and right in (0x9, 0xA):
        result = cmp_fn(0, right)
        return result

    try:
        if left.operation.name == "LLIL_AND" and left.right.constant == 1 and right == 0:
            return True
    except AttributeError:
        pass

    return None


def _controlling_if(ssa, block):
    """Among ``block``'s predecessors, the one terminating in the opaque-pred
    ``if`` that selects which edge reaches ``block``. Returns (block, if)."""
    for edge in block.incoming_edges:
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
    """Follow single-successor edges from ``start_block`` until a block whose
    successor is ``target_block`` -- the live predecessor on that path."""
    cur = start_block
    seen = set()
    while cur is not None and cur.start not in seen:
        seen.add(cur.start)
        if any(e.target.start == target_block.start for e in cur.outgoing_edges):
            return cur
        outs = cur.outgoing_edges
        cur = outs[0].target if len(outs) == 1 else None
    return None


def _live_phi_operand(bv, ssa, phi):
    """Pick the ``ϕ`` operand on the live path by evaluating the opaque
    predicate that controls the branch into the phi's block."""
    block = phi.il_basic_block
    if_block, if_instr = _controlling_if(ssa, block)
    if if_instr is None:
        if phi.address not in _warned_phi:
            _warned_phi.add(phi.address)
            log_warn(f"[phi] no controlling if for phi @ {hex(phi.address)} "
                     f"(block {block.start}, {len(block.incoming_edges)} preds)")
        return None
    truth = _eval_predicate(bv, ssa, if_instr)
    if truth is None:
        if phi.address not in _warned_phi:
            _warned_phi.add(phi.address)
            cond = _define_cond(ssa, if_instr.condition)
            log_warn(f"[phi] predicate unrecognized for phi @ {hex(phi.address)}: "
                     f"{cond.operation.name} -- {if_instr}")
        return None

    target_idx = if_instr.true if truth else if_instr.false
    target_block = ssa[target_idx].il_basic_block
    if target_block.start == block.start:
        live_pred = if_block          # predicate fell through straight to phi
    else:
        live_pred = _walk_to_pred(target_block, block)
    if live_pred is None:
        log_warn("live_pred is None")
        return None

    # The phi operand on the live edge is the reaching definition of the reg at
    # live_pred's exit: either defined directly in live_pred, or defined in a
    # block that dominates live_pred and flows through unchanged. (The other
    # operand reaches the phi via the *other* predecessor, so it never dominates
    # live_pred and is excluded.)
    live_doms = {b.start for b in live_pred.dominators}
    flow_through = []
    for var in phi.src:
        d = ssa.get_ssa_reg_definition(var)
        if d is None:
            continue
        db = d.il_basic_block
        if db.start == live_pred.start:
            return var                       # redefined right in the live pred
        if db.start in live_doms:
            flow_through.append(var)         # flows through from a dominator
    if len(flow_through) == 1:
        return flow_through[0]
    log_warn(f"phi operand undetermined at {hex(phi.address)}: {len(flow_through)} candidate(s)")
    return None


# --------------------------------------------------------------------------- #
# gadget parse + drive
# --------------------------------------------------------------------------- #

def _consts_preferring_live_path(bv, ssa, expr):
    value = _reg_const(bv, ssa, expr)
    return {value} if value is not None else _reg_consts(bv, ssa, expr)


def _slot_from_load(bv, ssa, sload):
    if sload is None or sload.operation.name not in ("LLIL_LOAD", "LLIL_LOAD_SSA"):
        return None
    sp = sload.src
    if sp.operation.name in ("LLIL_CONST_PTR", "LLIL_CONST"):
        return sp.constant & U48
    return _reg_const(bv, ssa, sp)


def _slot_add_const_candidates(bv, ssa, expr):
    expr = _def_src(ssa, expr)
    if expr is None or expr.operation.name != "LLIL_ADD":
        return []
    candidates = []
    for sload_expr, const_expr in ((expr.left, expr.right), (expr.right, expr.left)):
        slot = _slot_from_load(bv, ssa, _def_src(ssa, sload_expr))
        if slot is None:
            continue
        for value in _consts_preferring_live_path(bv, ssa, const_expr):
            candidates.append((slot, value & U48))
    return candidates


def _valid_offsets_for_candidate(bv, slot, table_base_key, key, offsets):
    valid = set()
    for offset in offsets:
        target = resolve_indirect_jump_addr(bv, slot, offset, table_base_key, key)
        if target is not None and bv.is_valid_offset(target):
            valid.add(offset & U48)
    return valid


def _is_index_offset(ssa, expr):
    expr = _def_src(ssa, expr)
    return expr is not None and expr.operation.name in ("LLIL_LSL", "LLIL_LSR")


def _parse_jump_gadget_parts(bv, ssa, jump_il):
    """
    
    Walk the decode gadget feeding ``jump_il`` backwards through LLIL SSA.

    Returns ``(slot, table_base_key, key, offsets)`` as ints, or ``None`` if the
    gadget does not match the expected shape."""

    jdest = jump_il.ssa_form.dest
    if jdest.operation.name != "LLIL_REG_SSA":
        return None

    # rax = rax (+/-) KEY -- the final decode step is `add` or `sub`; for `sub`
    # the effective key is negated (the U48 decode math treats it as addition).
    fin = _def_src(ssa, jdest)
    if fin is None or fin.operation.name not in ("LLIL_ADD", "LLIL_SUB"):
        return None
    if fin.operation.name == "LLIL_SUB":
        chain_expr, key_expr, neg = fin.left, fin.right, True
    else:
        # commutative: the chain operand leads to the entry LOAD; other is key.
        if _def_src(ssa, fin.left).operation.name in ("LLIL_LOAD", "LLIL_LOAD_SSA"):
            chain_expr, key_expr, neg = fin.left, fin.right, False
        else:
            chain_expr, key_expr, neg = fin.right, fin.left, False
    key = _reg_const(bv, ssa, key_expr)
    if key is None:
        return None
    if neg:
        key = -key

    # rax = [table_base_key + rax] -- entry load. The table_base_key (a register in
    # LLIL) and the table-base chain can sit on either side of the address add;
    # the chain is the operand whose definition is the OFFSET + [&SLOT] add.
    load = _def_src(ssa, chain_expr)
    if load is None or load.operation.name not in ("LLIL_LOAD", "LLIL_LOAD_SSA"):
        return None
    addr = load.src
    if addr.operation.name != "LLIL_ADD":
        return None

    # Some ARM64 samples pre-add the table-base key before indexing:
    #     target = *(*slot + table_base_key + offset) + key
    for tb, disp_expr in ((_def_src(ssa, addr.left), addr.right), (_def_src(ssa, addr.right), addr.left)):
        if not _is_index_offset(ssa, disp_expr):
            continue
        for slot, table_base_key in _slot_add_const_candidates(bv, ssa, tb):
            offsets = {o & U48 for o in _reg_consts(bv, ssa, disp_expr)}
            offsets = _valid_offsets_for_candidate(bv, slot, table_base_key, key, offsets)
            if offsets:
                return (slot, table_base_key, key & U48, offsets)

    cand = _def_src(ssa, addr.right)
    if cand is not None and cand.operation.name == "LLIL_ADD":
        disp_expr, tb = addr.left, cand
    else:
        tb = _def_src(ssa, addr.left)
        disp_expr = addr.right
    if tb is None or tb.operation.name != "LLIL_ADD":
        return None
    table_base_key = _reg_const(bv, ssa, disp_expr)
    if table_base_key is None:
        return None

    # rax = OFFSET + [&SLOT] -- table-base add. The [&SLOT] load (of a const
    # pointer) can be on either side; the other operand is the entry offset.
    tb_left = _def_src(ssa, tb.left)
    tb_right = _def_src(ssa, tb.right)
    if tb_right is not None and tb_right.operation.name in ("LLIL_LOAD", "LLIL_LOAD_SSA"):
        sload, off_expr = tb_right, tb.left
    else:
        sload, off_expr = tb_left, tb.right
    if sload.operation.name not in ("LLIL_LOAD", "LLIL_LOAD_SSA"):
        return None
    sp = sload.src
    if sp.operation.name in ("LLIL_CONST_PTR", "LLIL_CONST"):
        slot = sp.constant & U48
    else:
        slot = _reg_const(bv, ssa, sp)
    if slot is None:
        return None
    offsets = _reg_consts(bv, ssa, off_expr)
    if not offsets:
        return None

    return (slot, table_base_key & U48, key & U48, {o & U48 for o in offsets})


def parse_jump_gadget(bv, ssa, jump_il):
    """Return one decoded gadget tuple for compatibility with single-target callers."""
    parsed = _parse_jump_gadget_parts(bv, ssa, jump_il)
    if parsed is None:
        return None
    slot, table_base_key, key, offsets = parsed
    offset = sorted(offsets)[0]
    return (slot, table_base_key, key, offset)


def parse_jump_gadget_targets(bv, ssa, jump_il):
    """Return every decoded table target tuple for this branch gadget."""
    parsed = _parse_jump_gadget_parts(bv, ssa, jump_il)
    if parsed is None:
        return None
    slot, table_base_key, key, offsets = parsed
    return [(slot, table_base_key, key, offset) for offset in sorted(offsets)]


def iter_llil_indirect_jumps(llil):
    """
    Yield each unresolved decode-gadget terminator (jump or tail call through
    a register).

    If BN auto-defines a function at a gadget's decoded target, the gadget's
    own terminating ``jump(reg)`` gets reclassified as ``tailcall(reg)``; if we
    only matched ``LLIL_JUMP`` those gadgets would be silently skipped, so both
    forms are caught.
    """
    for block in llil:
        for insn in block:
            if insn.operation.name not in ("LLIL_JUMP", "LLIL_JUMP_TO", "LLIL_TAILCALL"):
                continue
            if insn.dest.operation.name == "LLIL_CONST_PTR":
                continue
            yield insn


def resolve_llil_jump_target(bv, ssa, jump_il):
    """Decode the concrete target of one decode-gadget ``LLIL_JUMP`` by parsing
    its gadget and decoding the jump table. Returns an ``int`` or ``None``."""
    targets = resolve_llil_jump_targets(bv, ssa, jump_il)
    return None if not targets else targets[0]


def resolve_llil_jump_targets(bv, ssa, jump_il):
    """Decode every concrete target for one decode-gadget branch."""
    parsed = parse_jump_gadget_targets(bv, ssa, jump_il)
    if parsed is None:
        log_warn(f"[gadget-llil] shape mismatch @ {hex(jump_il.address)}")
        return []
    targets = []
    for slot, table_base_key, key, offset in parsed:
        target = resolve_indirect_jump_addr(bv, slot, offset, table_base_key, key)
        log_debug(f"[gadget-llil] {hex(jump_il.address)} slot={hex(slot)} table_base_key={hex(table_base_key)} "
                  f"key={hex(key)} off={hex(offset)} -> "
                  f"{hex(target) if target is not None else None}")
        if target is not None:
            targets.append(target)
    return sorted(set(targets))


def resolve_llil_jump_plan(bv, llil, gadget_map=None):
    """Resolve decode-gadget branches to a plan without mutating BN state."""
    if not llil:
        return []
    if gadget_map is None:
        gadget_map = {}
    _warned_phi.clear()
    

    # Phase 1: resolve every target read-only against the original SSA. The
    # decode gadgets are independent, so none of these reads observe a later
    # rewrite -- batching avoids rebuilding SSA between jumps.
    ssa = llil.ssa_form
    pending = []
    for jump_il in iter_llil_indirect_jumps(llil):
        try:
            newly_resolved = jump_il.address not in gadget_map
            if jump_il.address in gadget_map:
                cached = gadget_map[jump_il.address]
                if isinstance(cached, (list, tuple, set)):
                    targets = list(cached)
                else:
                    targets = [cached]
            else:
                # Otherwise resolve it
                targets = resolve_llil_jump_targets(bv, ssa, jump_il)
            targets = [t for t in targets if t is not None and bv.is_valid_offset(t)]
            if not targets:
                continue
            pending.append({
                "source": jump_il.address,
                "dest_expr_index": jump_il.dest.expr_index,
                "targets": tuple(sorted(set(targets))),
                "newly_resolved": newly_resolved,
            })
        except Exception as e:  # noqa: BLE001
            log_error(f"[gadget-llil] {hex(jump_il.address)}: {e}")
            continue

    return pending


def apply_llil_jump_rewrites(bv, llil, plan):
    """Apply current-LLIL rewrites from a branch plan. Does not set user branches."""
    if not llil or not plan:
        return 0

    applied = 0
    for item in plan:
        jump_addr = item["source"]
        targets = item["targets"]
        try:
            if len(targets) == 1:
                new_dest = llil.const_pointer(bv.arch.address_size, targets[0])
                llil.replace_expr(item["dest_expr_index"], new_dest)
                applied += 1
            log_debug(
                f"[gadget-llil] {hex(jump_addr)} -> "
                f"{', '.join(hex(t) for t in targets)}"
            )
            for target in targets:
                existing = bv.get_function_at(target)
                if existing is not None and existing.start != llil.source_function.start:
                    bv.remove_user_function(existing)
        except Exception as e:  # noqa: BLE001
            log_error(f"[gadget-llil] {hex(jump_addr)}: {e}")
            continue

    if applied:
        llil.finalize()
        llil.generate_ssa_form()
    return applied


def resolve_and_rewrite_llil_jumps(bv, llil, gadget_map=None):
    """Compatibility wrapper: resolve and apply current-LLIL rewrites only."""
    plan = resolve_llil_jump_plan(bv, llil, gadget_map)
    apply_llil_jump_rewrites(bv, llil, plan)
    resolved = {}
    for item in plan:
        if item["newly_resolved"]:
            targets = item["targets"]
            resolved[item["source"]] = targets[0] if len(targets) == 1 else targets
    return resolved

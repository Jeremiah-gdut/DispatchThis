"""MLIL-stage global constant slot planner."""

from ...utils.log import log_info, log_warn


U48 = 0xFFFFFFFFFFFF
CONST_SLOT_TYPE = "uint8_t const* const"

_CONST_OPS = ("MLIL_CONST", "MLIL_CONST_PTR")
_LOAD_OPS = ("MLIL_LOAD", "MLIL_LOAD_SSA", "MLIL_LOAD_STRUCT", "MLIL_LOAD_STRUCT_SSA")
_SET_VAR_OPS = ("MLIL_SET_VAR", "MLIL_SET_VAR_FIELD")
_STORE_OPS = ("MLIL_STORE", "MLIL_STORE_SSA", "MLIL_STORE_STRUCT", "MLIL_STORE_STRUCT_SSA")


def _op(expr):
    return getattr(getattr(expr, "operation", None), "name", None)


def _single_var_def(mlil, var):
    try:
        defs = mlil.get_var_definitions(var)
    except Exception:  # noqa: BLE001
        return None
    return defs[0] if len(defs) == 1 else None


def _peel_var(mlil, expr):
    for _ in range(32):
        if _op(expr) != "MLIL_VAR":
            return expr
        d = _single_var_def(mlil, expr.src)
        if d is None or not hasattr(d, "src"):
            return expr
        expr = d.src
    return expr


def _const(mlil, expr):
    expr = _peel_var(mlil, expr)
    if _op(expr) in _CONST_OPS:
        return expr.constant
    return None


def _signed_const(value):
    return value - (1 << 64) if value > 0x7FFFFFFFFFFFFFFF else value


def _expr_const_addr(mlil, expr):
    expr = _peel_var(mlil, expr)
    c = _const(mlil, expr)
    if c is not None:
        return c & U48
    if _op(expr) == "MLIL_ADD":
        left = _expr_const_addr(mlil, expr.left)
        right = _expr_const_addr(mlil, expr.right)
        if left is not None and right is not None:
            return (left + right) & U48
    if _op(expr) == "MLIL_SUB":
        left = _expr_const_addr(mlil, expr.left)
        right = _expr_const_addr(mlil, expr.right)
        if left is not None and right is not None:
            return (left - right) & U48
    return None


def _walk_exprs(expr, seen=None):
    if expr is None:
        return
    if seen is None:
        seen = set()
    key = id(expr)
    if key in seen:
        return
    seen.add(key)
    yield expr
    for name in ("src", "dest", "left", "right", "condition"):
        child = getattr(expr, name, None)
        if hasattr(child, "operation"):
            yield from _walk_exprs(child, seen)
    for name in ("params", "output", "vars_read", "vars_written"):
        for child in getattr(expr, name, ()) or ():
            if hasattr(child, "operation"):
                yield from _walk_exprs(child, seen)


def _slot_uses(mlil):
    for ins in getattr(mlil, "instructions", ()) or ():
        ins_addr = getattr(ins, "address", 0)
        for expr in _walk_exprs(ins):
            yield expr, getattr(expr, "address", ins_addr)


def _slot_load_addr(mlil, expr):
    expr = _peel_var(mlil, expr)
    if _op(expr) not in _LOAD_OPS or getattr(expr, "size", None) != 8:
        return None
    return _expr_const_addr(mlil, expr.src)


def _slot_offsets(mlil, expr, depth=0):
    if depth > 32:
        return []
    expr = _peel_var(mlil, expr)
    slot_addr = _slot_load_addr(mlil, expr)
    if slot_addr is not None:
        return [(slot_addr, 0)]

    op = _op(expr)
    if op not in ("MLIL_ADD", "MLIL_SUB"):
        return []

    out = []
    right_const = _const(mlil, expr.right)
    if right_const is not None:
        right_const = _signed_const(right_const)
        addend = right_const if op == "MLIL_ADD" else -right_const
        out.extend(
            (slot, offset + addend)
            for slot, offset in _slot_offsets(mlil, expr.left, depth + 1)
        )

    if op == "MLIL_ADD":
        left_const = _const(mlil, expr.left)
        if left_const is not None:
            left_const = _signed_const(left_const)
            out.extend(
                (slot, offset + left_const)
                for slot, offset in _slot_offsets(mlil, expr.right, depth + 1)
            )

    return out


def _qword_at(bv, addr):
    data = bv.read(addr, 8)
    return int.from_bytes(data, "little") if len(data) == 8 else None


def _in_data_section(bv, addr):
    try:
        return any(section.name == ".data" for section in bv.get_sections_at(addr))
    except Exception:  # noqa: BLE001
        return False


def _plain_ptr_var(bv, addr):
    data_var = bv.get_data_var_at(addr)
    return data_var is not None and str(data_var.type).replace(" ", "") == "void*"


def _ref_functions(bv, data_var):
    seen = set()
    for ref in list(getattr(data_var, "code_refs", ()) or ()):
        funcs = []
        func = getattr(ref, "function", None)
        if func is not None:
            funcs = [func]
        else:
            try:
                funcs = list(bv.get_functions_containing(ref.address))
            except Exception:  # noqa: BLE001
                funcs = []
        for func in funcs:
            key = getattr(func, "start", id(func))
            if key in seen:
                continue
            seen.add(key)
            yield func


def _mlil_stores_slot(mlil, slot_addr):
    for ins in getattr(mlil, "instructions", ()) or ():
        for expr in _walk_exprs(ins):
            if _op(expr) not in _STORE_OPS:
                continue
            if _expr_const_addr(mlil, getattr(expr, "dest", None)) == slot_addr:
                return True
    return False


def _refs_store_slot(bv, current_mlil, slot_addr):
    data_var = bv.get_data_var_at(slot_addr)
    if _mlil_stores_slot(current_mlil, slot_addr):
        return True
    for func in _ref_functions(bv, data_var):
        mlil = getattr(func, "mlil", None)
        if (
            mlil is not None
            and mlil is not current_mlil
            and _mlil_stores_slot(mlil, slot_addr)
        ):
            return True
    return False


def _plan_for_slot(bv, mlil, slot_addr, offset, use_addr):
    if offset == 0:
        return None
    if not _plain_ptr_var(bv, slot_addr) or not _in_data_section(bv, slot_addr):
        return None
    value = _qword_at(bv, slot_addr)
    if value is None:
        return None
    resolved_addr = (value + offset) & U48
    if not bv.is_valid_offset(resolved_addr):
        return None
    if _refs_store_slot(bv, mlil, slot_addr):
        log_warn(f"[gconst] {hex(slot_addr)}: skipped, known reference writes to slot")
        return None
    return {
        "slot_addr": slot_addr,
        "type": CONST_SLOT_TYPE,
        "value": value,
        "resolved_addr": resolved_addr,
        "use_addr": use_addr,
    }


def plan_global_constant_slots(bv, mlil):
    """Find global constant slots whose data-var type should be made const."""
    if mlil is None:
        return []

    plans = {}
    for expr, use_addr in _slot_uses(mlil):
        for slot_addr, offset in _slot_offsets(mlil, expr):
            if slot_addr in plans:
                continue
            plan = _plan_for_slot(bv, mlil, slot_addr, offset, use_addr)
            if plan is not None:
                plans[slot_addr] = plan

    out = [plans[addr] for addr in sorted(plans)]
    if out:
        log_info(f"[gconst] planned {len(out)} global constant slot(s)")
    return out


def global_constant_cleanup_roots(mlil, slot_addrs):
    """SET_VAR instruction indices for direct loads from planned const slots."""
    slot_addrs = set(slot_addrs or ())
    if mlil is None or not slot_addrs:
        return set()

    roots = set()
    for ins in getattr(mlil, "instructions", ()) or ():
        if _op(ins) not in _SET_VAR_OPS:
            continue
        if _slot_load_addr(mlil, getattr(ins, "src", None)) not in slot_addrs:
            continue
        instr_index = getattr(ins, "instr_index", None)
        if instr_index is not None:
            roots.add(instr_index)
    return roots

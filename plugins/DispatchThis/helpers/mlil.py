"""MLIL profile helpers."""

from .memory import read_uint_le


U64 = 0xFFFFFFFFFFFFFFFF
U48 = 0xFFFFFFFFFFFF

CONST_OPS = ("MLIL_CONST", "MLIL_CONST_PTR")
LOAD_OPS = ("MLIL_LOAD", "MLIL_LOAD_SSA", "MLIL_LOAD_STRUCT")
LOAD_STRUCT_OPS = ("MLIL_LOAD_STRUCT", "MLIL_LOAD_STRUCT_SSA")
SLOT_LOAD_OPS = ("MLIL_LOAD", "MLIL_LOAD_SSA", *LOAD_STRUCT_OPS)
SET_VAR_OPS = ("MLIL_SET_VAR", "MLIL_SET_VAR_FIELD")
STORE_OPS = ("MLIL_STORE", "MLIL_STORE_SSA", "MLIL_STORE_STRUCT", "MLIL_STORE_STRUCT_SSA")


def walk_expr(expr):
    if expr is None:
        return []
    try:
        return list(expr.traverse(lambda node: node))
    except AttributeError:
        pass

    out = []
    seen = set()

    def visit(node):
        if node is None or id(node) in seen:
            return
        seen.add(id(node))
        out.append(node)
        for name in ("src", "dest", "left", "right", "condition"):
            child = getattr(node, name, None)
            if hasattr(child, "operation"):
                visit(child)
        for name in ("params", "output", "vars_read", "vars_written"):
            for child in getattr(node, name, ()) or ():
                if hasattr(child, "operation"):
                    visit(child)

    visit(expr)
    return out


def _op_name(expr):
    return getattr(getattr(expr, "operation", None), "name", None)


def constant_value(mlil, expr):
    expr = peel_var_definitions(
        mlil,
        expr,
        max_depth=32,
        require_single=True,
        allowed_ops=None,
    )
    return expr.constant if _op_name(expr) in CONST_OPS else None


def constant_address(mlil, expr, depth=0, max_depth=32):
    if expr is None or depth > max_depth:
        return None
    expr = peel_var_definitions(
        mlil,
        expr,
        max_depth=max_depth,
        require_single=True,
        allowed_ops=None,
    )
    value = constant_value(mlil, expr)
    if value is not None:
        return value & U48
    op = _op_name(expr)
    if op in ("MLIL_ADD", "MLIL_SUB"):
        left = constant_address(mlil, expr.left, depth + 1, max_depth)
        right = constant_address(mlil, expr.right, depth + 1, max_depth)
        if left is not None and right is not None:
            return (left + right if op == "MLIL_ADD" else left - right) & U48
    return None


def load_slot_address(mlil, expr, width=8):
    expr = peel_var_definitions(
        mlil,
        expr,
        max_depth=32,
        require_single=True,
        allowed_ops=None,
    )
    op = _op_name(expr)
    if op not in SLOT_LOAD_OPS or getattr(expr, "size", None) != width:
        return None
    addr = constant_address(mlil, expr.src)
    if addr is None:
        return None
    if op in LOAD_STRUCT_OPS:
        offset = getattr(expr, "offset", 0)
        if not isinstance(offset, int):
            return None
        return (addr + offset) & U48
    return addr


def mlil_stores_to_address(mlil, addr):
    for ins in getattr(mlil, "instructions", ()) or ():
        for expr in walk_expr(ins):
            if (
                _op_name(expr) in STORE_OPS
                and constant_address(mlil, getattr(expr, "dest", None)) == addr
            ):
                return True
    return False


def iter_indirect_calls(mlil):
    """Yield MLIL call instructions whose destination is not already direct."""
    if mlil is None:
        return
    for insn in mlil.instructions:
        if not insn.operation.name.startswith("MLIL_CALL"):
            continue
        if insn.dest.operation.name in CONST_OPS:
            continue
        yield insn


def peel_var_definitions(
    mlil,
    expr,
    trail=None,
    max_depth=64,
    require_single=False,
    allowed_ops=SET_VAR_OPS,
):
    """Follow MLIL_VAR through SET_VAR definitions and return the peeled expr."""
    for _ in range(max_depth):
        if expr is None or expr.operation.name != "MLIL_VAR":
            break
        try:
            defs = mlil.get_var_definitions(expr.src)
        except Exception:  # noqa: BLE001
            break
        if not defs or (require_single and len(defs) != 1):
            break
        definition = defs[0]
        if allowed_ops is not None and definition.operation.name not in allowed_ops:
            break
        if not hasattr(definition, "src"):
            break
        if trail is not None:
            trail.append(definition)
        expr = definition.src
    return expr


def _single_value(expr):
    try:
        value = expr.value
    except Exception:  # noqa: BLE001
        return None
    if value.type.name in ("ConstantValue", "ConstantPointerValue", "ImportedAddressValue"):
        return value.value & U64
    return None


def fold_constant_value(bv, mlil, expr, depth=0, max_depth=32):
    """Best-effort single-value fold for current MLIL call-target recovery."""
    if expr is None or depth > max_depth:
        return None
    op = expr.operation.name

    if op in CONST_OPS:
        return expr.constant & U64

    if op == "MLIL_VAR":
        try:
            defs = mlil.get_var_definitions(expr.src)
        except Exception:  # noqa: BLE001
            defs = ()
        if defs and defs[0].operation.name in SET_VAR_OPS:
            value = fold_constant_value(bv, mlil, defs[0].src, depth + 1, max_depth)
            if value is not None:
                return value
        return _single_value(expr)

    if op in ("MLIL_ADD", "MLIL_SUB"):
        left = fold_constant_value(bv, mlil, expr.left, depth + 1, max_depth)
        right = fold_constant_value(bv, mlil, expr.right, depth + 1, max_depth)
        if left is None or right is None:
            return None
        return (left + right if op == "MLIL_ADD" else left - right) & U64

    if op == "MLIL_MUL":
        left = fold_constant_value(bv, mlil, expr.left, depth + 1, max_depth)
        right = fold_constant_value(bv, mlil, expr.right, depth + 1, max_depth)
        return None if left is None or right is None else (left * right) & U64

    if op in ("MLIL_ZX", "MLIL_SX", "MLIL_LOW_PART"):
        return fold_constant_value(bv, mlil, expr.src, depth + 1, max_depth)

    if op in LOAD_OPS:
        addr = fold_constant_value(bv, mlil, expr.src, depth + 1, max_depth)
        if addr is None:
            return None
        return read_uint_le(bv, addr & U48, expr.size)

    return _single_value(expr)


def cleanup_roots_for_expr(mlil, expr):
    """Instruction indices defining MLIL vars read by ``expr``."""
    roots = set()
    for node in walk_expr(expr):
        if node.operation.name != "MLIL_VAR":
            continue
        try:
            defs = mlil.get_var_definitions(node.src)
        except Exception:  # noqa: BLE001
            continue
        for definition in defs:
            if definition.operation.name in SET_VAR_OPS:
                roots.add(definition.instr_index)
    return roots


def set_roots_before(mlil, site_addrs):
    """Contiguous pure assignment instruction indices before owned sites."""
    site_addrs = set(site_addrs or ())
    roots = set()
    if mlil is None or not site_addrs:
        return roots

    for block in mlil.basic_blocks:
        block_instrs = [mlil[i] for i in range(block.start, block.end)]
        for pos, ins in enumerate(block_instrs):
            if ins.address not in site_addrs:
                continue
            for prev in reversed(block_instrs[:pos]):
                if prev.operation.name not in SET_VAR_OPS:
                    break
                roots.add(prev.instr_index)
    return roots


__all__ = (
    "CONST_OPS",
    "LOAD_STRUCT_OPS",
    "LOAD_OPS",
    "SET_VAR_OPS",
    "SLOT_LOAD_OPS",
    "STORE_OPS",
    "cleanup_roots_for_expr",
    "constant_address",
    "constant_value",
    "fold_constant_value",
    "iter_indirect_calls",
    "load_slot_address",
    "mlil_stores_to_address",
    "peel_var_definitions",
    "set_roots_before",
    "walk_expr",
)

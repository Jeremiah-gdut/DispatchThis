"""MLIL profile helpers."""

from .memory import read_uint_le


U64 = 0xFFFFFFFFFFFFFFFF
U48 = 0xFFFFFFFFFFFF

CONST_OPS = ("MLIL_CONST", "MLIL_CONST_PTR")
LOAD_OPS = ("MLIL_LOAD", "MLIL_LOAD_SSA", "MLIL_LOAD_STRUCT")
SET_VAR_OPS = ("MLIL_SET_VAR", "MLIL_SET_VAR_FIELD")


def walk_expr(expr):
    if expr is None:
        return []
    return list(expr.traverse(lambda node: node))


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


def peel_var_definitions(mlil, expr, trail=None, max_depth=64):
    """Follow MLIL_VAR through SET_VAR definitions and return the peeled expr."""
    for _ in range(max_depth):
        if expr is None or expr.operation.name != "MLIL_VAR":
            break
        try:
            defs = mlil.get_var_definitions(expr.src)
        except Exception:  # noqa: BLE001
            break
        if not defs or defs[0].operation.name not in SET_VAR_OPS:
            break
        if trail is not None:
            trail.append(defs[0])
        expr = defs[0].src
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
    "LOAD_OPS",
    "SET_VAR_OPS",
    "cleanup_roots_for_expr",
    "fold_constant_value",
    "iter_indirect_calls",
    "peel_var_definitions",
    "set_roots_before",
    "walk_expr",
)
